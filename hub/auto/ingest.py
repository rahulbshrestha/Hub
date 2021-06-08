from hub.auto.converter import Converter
import os


from hub import Dataset
from hub.util.kaggle import download_kaggle

import warnings


def from_path(unstructured_path: str, **kwargs):
    """Creates a hub dataset from unstructured data.

    Note:
        This copies the data into hub format.
        Be careful when using this with large datasets.

    Args:
        path (str): Path to the data to be converted

    Returns:
        A Dataset instance whose path points to the hub formatted
        copy of the data.
    """


    if "mode" in kwargs:
        warnings.warn("Mode should not be passed to `Dataset.from_path`. Using write mode.")

    ds = Dataset(**kwargs, mode="w")

    converter = Converter(unstructured_path)
    converter.from_image_classification(ds)

    # TODO: opt-in delete unstructured data after ingestion

    return ds


# TODO: rename `credentials` -> `kaggle_credentials`
def from_kaggle(tag: str, path: str, local_path: str=None, credentials: dict={}, **kwargs):
    # TODO: docstring
    # TODO: make sure path and local path are not equal
    # TODO: make path variable names more obvious

    if not local_path:
        local_path = os.path.join(path, "unstructured")

    download_kaggle(tag, local_path, credentials=credentials)
    ds = from_path(local_path, path=path, **kwargs)

    return ds