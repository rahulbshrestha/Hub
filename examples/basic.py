from hub import Dataset, features
import numpy as np
from hub import dev_mode


def main():
    # Tag is set {Username}/{Dataset}
    tag = "davitb/basic11"

    # Create dataset
    ds = Dataset(
        tag,
        shape=(4,),
        schema={
            "image": features.Tensor((512, 512), dtype="float"),
            "label": features.Tensor((512, 512), dtype="float"),
        },
    )

    # Upload Data
    ds["image"][:] = np.ones((4, 512, 512))
    ds["label"][:] = np.ones((4, 512, 512))
    ds.commit()

    # Load the data
    ds = Dataset(tag)
    print(ds["image"][0].compute())


if __name__ == "__main__":
    main()
