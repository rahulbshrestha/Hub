from hub.core.compression import decompress_array
from math import ceil
from typing import Optional, Sequence, Union, Tuple, List, Set
from hub.util.exceptions import (
    CorruptedMetaError,
    DynamicTensorNumpyError,
)
from hub.core.meta.tensor_meta import TensorMeta
from hub.core.meta.hashlist import Hashlist
from hub.core.index.index import Index
from hub.util.keys import (
    get_chunk_key,
    get_chunk_id_encoder_key,
    get_tensor_meta_key,
    get_hashlist_key,
)
from hub.core.sample import Sample  # type: ignore
from hub.constants import DEFAULT_MAX_CHUNK_SIZE

import numpy as np

from hub.core.storage.lru_cache import LRUCache

from hub.core.chunk import Chunk

from hub.core.meta.encode.chunk_id import ChunkIdEncoder

import mmh3, os, sys


SampleValue = Union[np.ndarray, int, float, bool, Sample]


def is_uniform_sequence(samples):
    """Determines if a sequence of samples has uniform type and shape, allowing it to be vectorized by `ChunkEngine.extend`."""
    if len(set(map(type, samples))) != 1:
        # Cannot vectorize sequence with inconsistent types
        return False
    elif any(isinstance(s, np.ndarray) for s in samples):
        # Numpy arrays will only be vectorized if they have the same shape
        return len(set(s.shape for s in samples)) == 1
    elif any(isinstance(s, Sample) for s in samples):
        # Sample objects will not be vectorized
        return False
    else:
        # Scalar samples can be vectorized
        return True


"""Generates a 128 bit hash for a sample using murmurhash3"""


def hash_sample(sample):
    hash = mmh3.hash_bytes(sample)
    return hash.hex()


class ChunkEngine:
    def __init__(
        self,
        key: str,
        cache: LRUCache,
        max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE,
        meta_cache: LRUCache = None,
    ):
        """Handles creating `Chunk`s and filling them with incoming samples.

        Data delegation:
            All samples must live inside a chunk. No chunks may contain partial samples, only 1 chunk per sample.
            A chunk holds the dynamic information for the samples they contain (like shape and byte ranges).
            For more information on the `Chunk` format, check out the `Chunk` class.

        ChunkIdEncoder:
            The `ChunkIdEncoder` bidirectionally maps samples to the chunk IDs they live in. For more information,
            see `ChunkIdEncoder`'s docstring.

        Example:
            Given:
                Sample sizes: [1 * MB, 1 * MB, 14 * MB, 15 * MB, 15 * MB]
                Min chunk size: 16 * MB
                Max chunk size: 32 * MB


            Basic logic:
                >>> chunks = []
                >>> chunks.append(sum([1 * MB, 1 * MB, 14 * MB, 15 * MB]))  # i=(0, 1, 2, 3)
                >>> chunks[-1]
                31 * MB
                >>> chunks.append(sum([15 * MB]))  # i=(4,)
                >>> chunks[-1]
                15 * MB

            Samples 0, 1, 2, and 3 can be stored in 1 chunk. sample 4 resides in it's own chunk.

            If more samples come later: sizes = [15 * MB, 1 * MB]

            Basic logic:
                >>> len(chunks)
                2
                >>> chunks[-1]
                15 * MB
                >>> chunks[-1] += sum([15 * MB, 1 * MB])  # i=(5, 6)
                >>> chunks[-1]
                31 * MB
                >>> sum(chunks)
                62 * MB
                >>> len(chunks)
                2

            Because our max chunk size is 32 * MB, we try to fit as much data into this size as possible.


        Args:
            key (str): Tensor key.
            cache (LRUCache): Cache for which chunks and the metadata are stored.
            max_chunk_size (int): Chunks generated by this instance will never exceed this size. Defaults to DEFAULT_MAX_CHUNK_SIZE.
            meta_cache (LRUCache): Cache used for storing non chunk data such as tensor meta and chunk id encoder during transforms in memory.

        Raises:
            ValueError: If invalid max chunk size.
        """

        self.key = key
        self.cache = cache

        if max_chunk_size <= 2:
            raise ValueError("Max chunk size should be > 2 bytes.")

        self.max_chunk_size = max_chunk_size

        # only the last chunk may be less than this
        self.min_chunk_size = self.max_chunk_size // 2
        self._meta_cache = meta_cache

    @property
    def meta_cache(self) -> LRUCache:
        return self._meta_cache or self.cache

    @property
    def chunk_id_encoder(self) -> ChunkIdEncoder:
        """Gets the chunk id encoder from cache, if one is not found it creates a blank encoder.
        For more information on what `ChunkIdEncoder` is used for, see the `__init__` docstring.

        Raises:
            CorruptedMetaError: If chunk id encoding was corrupted.

        Returns:
            ChunkIdEncoder: The chunk ID encoder handles the mapping between sample indices
                and their corresponding chunks.
        """
        key = get_chunk_id_encoder_key(self.key)
        if not self.chunk_id_encoder_exists:

            # 1 because we always update the meta information before writing the samples (to account for potentially corrupted data in the future)
            if self.tensor_meta.length > 1:
                raise CorruptedMetaError(
                    f"Tensor length is {self.tensor_meta.length}, but could not find the chunk id encoder."
                )

            enc = ChunkIdEncoder()
            self.meta_cache[key] = enc
            return enc

        enc = self.meta_cache.get_cachable(key, ChunkIdEncoder)
        return enc

    @property
    def chunk_id_encoder_exists(self) -> bool:
        try:
            key = get_chunk_id_encoder_key(self.key)
            self.meta_cache[key]
            return True
        except KeyError:
            return False

    @property
    def num_chunks(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_chunks

    @property
    def num_samples(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_samples

    @property
    def last_chunk(self) -> Optional[Chunk]:
        if self.num_chunks == 0:
            return None

        return self.cache.get_cachable(self.last_chunk_key, Chunk)

    @property
    def last_chunk_key(self) -> str:
        last_chunk_name = self.chunk_id_encoder.get_name_for_chunk(-1)
        last_chunk_key = get_chunk_key(self.key, last_chunk_name)
        return last_chunk_key

    @property
    def tensor_meta(self):
        tensor_meta_key = get_tensor_meta_key(self.key)
        return self.meta_cache.get_cachable(tensor_meta_key, TensorMeta)

    @property
    def hashlist(self):
        hashlist_key = get_hashlist_key(self.key)
        return self.cache.get_cachable(hashlist_key, Hashlist)

    def _append_bytes(self, buffer: memoryview, shape: Tuple[int], dtype: np.dtype):
        """Treat `buffer` as a single sample and place them into `Chunk`s. This function implements the algorithm for
        determining which chunks contain which parts of `buffer`.

        Args:
            buffer (memoryview): Buffer that represents a single sample. Can have a
                length of 0, in which case `shape` should contain at least one 0 (empty sample).
            shape (Tuple[int]): Shape for the sample that `buffer` represents.
            dtype (np.dtype): Data type for the sample that `buffer` represents.
        """
        self.cache.check_readonly()
        # num samples is always 1 when appending
        num_samples = 1

        # update tensor meta first because erroneous meta information is better than un-accounted for data.
        buffer = self.tensor_meta.adapt(buffer, shape, dtype)
        self.tensor_meta.update(shape, dtype, num_samples)

        buffer_consumed = self._try_appending_to_last_chunk(buffer, shape)
        if not buffer_consumed:
            self._append_to_new_chunk(buffer, shape)

        self.chunk_id_encoder.register_samples(num_samples)
        self._synchronize_cache()

    def _synchronize_cache(self):
        """Synchronizes cachables with the cache. Includes: the last chunk, tensor meta, and chunk IDs encoder."""

        # TODO implement tests for cache size compute
        # TODO: optimize this by storing all of these keys in the chunk engine's state (posixpath.joins are pretty slow)
        # synchronize last chunk
        last_chunk_key = self.last_chunk_key
        last_chunk = self.last_chunk
        self.cache.update_used_cache_for_path(last_chunk_key, last_chunk.nbytes)  # type: ignore

        # synchronize tensor meta
        tensor_meta_key = get_tensor_meta_key(self.key)
        self.meta_cache[tensor_meta_key] = self.tensor_meta

        # synchronize chunk ID encoder
        chunk_id_key = get_chunk_id_encoder_key(self.key)
        self.meta_cache[chunk_id_key] = self.chunk_id_encoder

    def _try_appending_to_last_chunk(
        self, buffer: memoryview, shape: Tuple[int]
    ) -> bool:
        """Will store `buffer` inside of the last chunk if it can.
        It can be stored in the last chunk if it exists and has space for `buffer`.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.
            shape (Tuple[int]): Shape for the sample that `buffer` represents.

        Returns:
            bool: True if `buffer` was successfully written to the last chunk, otherwise False.
        """

        last_chunk = self.last_chunk
        if last_chunk is None:
            return False

        incoming_num_bytes = len(buffer)

        if last_chunk.is_under_min_space(self.min_chunk_size):
            last_chunk_size = last_chunk.num_data_bytes
            chunk_ct_content = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes
            )

            extra_bytes = min(incoming_num_bytes, self.max_chunk_size - last_chunk_size)
            combined_chunk_ct = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes + last_chunk_size
            )

            # combine if count is same
            if combined_chunk_ct == chunk_ct_content:
                last_chunk.append_sample(
                    buffer[:extra_bytes], self.max_chunk_size, shape
                )
                return True

        return False

    def _append_to_new_chunk(self, buffer: memoryview, shape: Tuple[int]):
        """Will create a new chunk and store `buffer` inside of it. Assumes that `buffer`'s length is < max chunk size.
        This should be called if `buffer` could not be added to the last chunk.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.
            shape (Tuple[int]): Shape for the sample that `buffer` represents.
        """

        # check if `last_chunk_extended` to handle empty samples
        new_chunk = self._create_new_chunk()
        new_chunk.append_sample(buffer, self.max_chunk_size, shape)

    def _create_new_chunk(self):
        """Creates and returns a new `Chunk`. Automatically creates an ID for it and puts a reference in the cache."""

        chunk_id = self.chunk_id_encoder.generate_chunk_id()
        chunk = Chunk()
        chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
        chunk_key = get_chunk_key(self.key, chunk_name)
        self.cache[chunk_key] = chunk
        return chunk

    def extend(self, samples: Union[np.ndarray, Sequence[SampleValue]]):
        """Formats a batch of `samples` and feeds them into `_append_bytes`."""

        hash_samples = self.tensor_meta.hash_samples

        if isinstance(samples, np.ndarray):
            compression = self.tensor_meta.sample_compression
            if compression is None:

                buffers = []

                # before adding any data, we need to check all sample sizes
                for sample in samples:

                    if hash_samples:
                        hash_value = hash_sample(sample.tobytes())
                        self.hashlist.append(hash_value)

                    buffer = memoryview(sample.tobytes())
                    self._check_sample_size(len(buffer))
                    buffers.append(buffer)

                for buffer in buffers:
                    self._append_bytes(buffer, sample.shape, sample.dtype)
            else:
                sample_objects = []
                compression = self.tensor_meta.sample_compression

                # before adding any data, we need to check all sample sizes
                for sample in samples:
                    sample_object = Sample(array=sample)
                    sample_objects.append(sample_object)

                    if hash_samples:
                        hash_value = hash_sample(sample.uncompressed_bytes())
                        self.hashlist.append(hash_value)

                    num_bytes = len(sample_object.compressed_bytes(compression))
                    self._check_sample_size(num_bytes)

                for sample_object in sample_objects:
                    self.append(sample_object)

        elif isinstance(samples, Sequence):
            if is_uniform_sequence(samples):
                self.extend(np.array(samples))
            else:
                for sample in samples:
                    self.append(sample)
        else:
            raise TypeError(f"Unsupported type for extending. Got: {type(samples)}")

        self.cache.maybe_flush()

    def append(self, sample: SampleValue):
        """Formats a single `sample` (compresseses/decompresses if applicable) and feeds it into `_append_bytes`."""

        hash_samples = self.tensor_meta.hash_samples

        if isinstance(sample, Sample):
            # has to decompress to read the array's shape and dtype
            # might be able to optimize this away
            compression = self.tensor_meta.sample_compression
            data = memoryview(sample.compressed_bytes(compression))
            self._check_sample_size(len(data))

            if hash_samples:
                hash_value = hash_sample(sample.uncompressed_bytes())
                self.hashlist.append(hash_value)

            self._append_bytes(data, sample.shape, sample.dtype)

        else:
            return self.append(Sample(array=np.array(sample)))

        self.cache.maybe_flush()

    def numpy(
        self, index: Index, aslist: bool = False
    ) -> Union[np.ndarray, Sequence[np.ndarray]]:
        """Reads samples from chunks and returns as a numpy array. If `aslist=True`, returns a sequence of numpy arrays.

        Args:
            index (Index): Represents the samples to read from chunks. See `Index` for more information.
            aslist (bool): If True, the samples will be returned as a list of numpy arrays. If False, returns a single numpy array. Defaults to False.

        Raises:
            DynamicTensorNumpyError: If shapes of the samples being read are not all the same.

        Returns:
            Union[np.ndarray, Sequence[np.ndarray]]: Either a list of numpy arrays or a single numpy array (depending on the `aslist` argument).
        """

        length = self.num_samples
        enc = self.chunk_id_encoder
        last_shape = None
        samples = []

        for global_sample_index in index.values[0].indices(length):
            chunk_id = enc[global_sample_index]
            chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
            chunk_key = get_chunk_key(self.key, chunk_name)
            chunk = self.cache.get_cachable(chunk_key, Chunk)
            sample = self.read_sample_from_chunk(global_sample_index, chunk)
            shape = sample.shape

            if not aslist and last_shape is not None:
                if shape != last_shape:
                    raise DynamicTensorNumpyError(self.key, index, "shape")

            samples.append(sample)
            last_shape = shape

        return _format_samples(samples, index, aslist)

    def read_sample_from_chunk(
        self, global_sample_index: int, chunk: Chunk
    ) -> np.ndarray:
        """Read a sample from a chunk, converts the global index into a local index. Handles decompressing if applicable."""

        expect_compressed = self.tensor_meta.sample_compression is not None
        dtype = self.tensor_meta.dtype

        enc = self.chunk_id_encoder

        buffer = chunk.memoryview_data
        local_sample_index = enc.translate_index_relative_to_chunks(global_sample_index)
        shape = chunk.shapes_encoder[local_sample_index]
        sb, eb = chunk.byte_positions_encoder[local_sample_index]

        buffer = buffer[sb:eb]
        if expect_compressed:
            sample = decompress_array(buffer, shape)
        else:
            sample = np.frombuffer(buffer, dtype=dtype).reshape(shape)

        return sample

    def _check_sample_size(self, num_bytes: int):
        if num_bytes > self.min_chunk_size:
            msg = f"Sorry, samples that exceed minimum chunk size ({self.min_chunk_size} bytes) are not supported yet (coming soon!). Got: {num_bytes} bytes."

            if self.tensor_meta.sample_compression is None:
                msg += "\nYour data is uncompressed, so setting `sample_compression` in `Dataset.create_tensor` could help here!"

            raise NotImplementedError(msg)

    def get_chunk_names(
        self, sample_index: int, last_index: int, target_chunk_count: int
    ) -> Set[str]:
        """Fetches a set of chunk names in which data starting from sample_index is contained.
            This is used by Pytorch integration.

        Args:
            sample_index: The index starting from which chunk names need to be fetched.
            last_index: The last index till which chunk names need to be fetched.
            target_chunk_count: The target number of chunk names required. The actual size of the returned set may be:-
                a) Less than target_chunk_count: If there are no more chunks to fetch.
                b) More than target_chunk_count: If the last chunk filling up target_chunk_count is a partial chunk, the remaining chunks are fetched.
                c) Equal to the target_chunk_count: In all other cases.

        Returns:
            Set of chunk names.
        """
        chunk_names: Set[str] = set()
        while len(chunk_names) < target_chunk_count and sample_index < last_index:
            chunk_id = self.chunk_id_encoder[sample_index]
            chunk = self.chunk_id_encoder.name_from_id(chunk_id)
            # todo, change to chunk_names.update once chunks returns sequence instead of single string
            chunk_names.add(chunk)
            sample_index += 1
        return chunk_names

    def validate_num_samples_is_synchronized(self):
        """Check if tensor meta length and chunk ID encoder are representing the same number of samples.
        Helpful for determining if a user has tampered with the tensor meta or the chunk ID encoder, or if
        the tensor was corruptd.

        Raises:
            CorruptedMetaError: tensor_meta and chunk_id_encoder must have the same num samples.
        """

        tensor_meta_length = self.tensor_meta.length

        # compare chunk ID encoder and tensor meta
        chunk_id_num_samples = self.chunk_id_encoder.num_samples
        if tensor_meta_length != chunk_id_num_samples:
            tkey = get_tensor_meta_key(self.key)
            ikey = get_chunk_id_encoder_key(self.key)
            raise CorruptedMetaError(
                f"'{tkey}' and '{ikey}' have a record of different numbers of samples. Got {tensor_meta_length} and {chunk_id_num_samples} respectively."
            )


def _format_samples(
    samples: Sequence[np.array], index: Index, aslist: bool
) -> Union[np.ndarray, List[np.ndarray]]:
    """Helper function for preparing `samples` read from the chunk engine in the way the format the user expects."""

    samples = index.apply(samples)  # type: ignore

    if aslist and all(map(np.isscalar, samples)):
        samples = list(arr.item() for arr in samples)

    samples = index.apply_squeeze(samples)  # type: ignore

    if aslist:
        return samples
    else:
        return np.array(samples)


def _min_chunk_ct_for_data_size(chunk_max_data_bytes: int, size: int) -> int:
    """Calculates the minimum number of chunks in which data of given size can be fit."""
    return ceil(size / chunk_max_data_bytes)
