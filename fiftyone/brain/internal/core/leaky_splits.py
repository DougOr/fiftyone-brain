"""
Finds leaks between splits.
"""

from collections import defaultdict
from copy import copy

import fiftyone as fo
from fiftyone import ViewField as F

# pylint: disable=no-member
import cv2

import fiftyone.core.brain as fob
import fiftyone.brain.similarity as sim
import fiftyone.brain.internal.core.sklearn as skl_sim
import fiftyone.brain.internal.core.duplicates as dups
import fiftyone.brain.internal.core.utils as fbu
import fiftyone.core.utils as fou


def compute_leaky_splits(
    samples,
    split_tags,
    method="similarity",
    similarity_backend=None,
    similarity_backend_kwargs=None,
    **kwargs,
):
    print("bar")


### GENERAL


class LeakySplitsConfigInterface(object):
    """Configuration for Leaky Splits

    Args:
        split_views (None): list of views corresponding to different splits
        split_field (None): field name that contains the split that the sample belongs to
        split_tags (None): list of tags that correspond to different splits
    """

    def __init__(
        self, split_views=None, split_field=None, split_tags=None, **kwargs
    ):
        self.split_views = split_views
        self.split_field = split_field
        self.split_tags = split_tags
        super().__init__(**kwargs)


class LeakySplitIndexInterface(object):
    def __init__(self) -> None:
        pass

    @property
    def num_leaks(self):
        return self.leaks.count

    @property
    def leaks(self):
        """
        Returns view with all potential leaks.
        """
        pass

    def leaks_by_sample(self, sample):
        """
        Return view with all leaks related to a certain sample.
        """
        pass

    def remove_leaks(self, remove_from):
        """Remove leaks from dataset

        Args:
            remove_from: tag/field value/view to remove from (e.g. remove the leak from 'test')
        """
        pass

    def tag_leaks(self, tag="leak"):
        """Tag leaks"""
        for s in self.leaks.iter_samples():
            s.tags.append(tag)
            s.save()


def _to_views(samples, split_views=None, split_field=None, split_tags=None):
    """Helper function so that we can always work with views"""

    arithmetic_true = lambda x: int(x is not None)
    num_given = (
        arithmetic_true(split_views)
        + arithmetic_true(split_field)
        + arithmetic_true(split_tags)
    )

    if num_given == 0:
        raise ValueError(f"One of the split arguments must be given.")
    if num_given > 1:
        raise ValueError(f"Only one of the split arguments must be given.")

    if split_views:
        return split_views

    if split_field:
        return _field_to_views(samples, split_field)

    if split_tags:
        return _tags_to_views(samples, split_tags)


def _field_to_views(samples, field):
    field_values = samples.distinct(field)

    if len(field_values) < 2:
        raise ValueError(
            f"Field {field} has less than 2 distinct values,"
            f"can't be used to create splits"
        )

    views = []
    for val in field_values:
        view = samples.match(F(field) == val)
        views.append(view)

    return views


def _tags_to_views(samples, tags):
    if len(tags) < 2:
        raise ValueError("Must provide at least two tags.")

    views = []
    for tag in tags:
        view = samples.match_tags([tag])
        views.append(view)
    return views


###

### SKL BACKEND
class LeakySplitsSKLConfig(
    skl_sim.SklearnSimilarityConfig, LeakySplitsConfigInterface
):
    """Configuration for Leaky Splits with the SKLearn backend

    Args:
        split_views (None): list of views corresponding to different splits
        split_field (None): field name that contains the split that the sample belongs to
        split_tags (None): list of tags that correspond to different splits
    """

    def __init__(
        self,
        split_views=None,
        split_field=None,
        split_tags=None,
        embeddings_field=None,
        model=None,
        patches_field=None,
        supports_prompts=None,
        metric="cosine",
        **kwargs,
    ):
        LeakySplitsConfigInterface.__init__(
            self, split_views, split_field, split_tags
        )
        skl_sim.SklearnSimilarityConfig.__init__(
            self,
            embeddings_field=embeddings_field,
            model=model,
            patches_field=patches_field,
            supports_prompts=supports_prompts,
            metric=metric,
            **kwargs,
        )

    @property
    def method(self):
        return "Neural"


class LeakySplitsSKL(skl_sim.SklearnSimilarity):
    def initialize(self, samples, brain_key):
        return LeakySplitsSKLIndex(
            samples, self.config, brain_key, backend=self
        )


class LeakySplitsSKLIndex(
    skl_sim.SklearnSimilarityIndex, LeakySplitIndexInterface
):
    def __init__(self, samples, config, brain_key, **kwargs):
        skl_sim.SklearnSimilarityIndex.__init__(
            self, samples=samples, config=config, brain_key=brain_key, **kwargs
        )
        self.split_views = _to_views(
            samples,
            self.config.split_views,
            self.config.split_field,
            self.config.split_tags,
        )
        self._leak_threshold = 1
        self._cached_leaks_view = None

    def set_threshold(self, threshold):
        self._leak_threshold = threshold

    @property
    def leaks(self):
        """
        Returns view with all potential leaks.
        """

        if self._cached_leaks_view:
            return self._cached_leaks_view

        embeddings, sample_ids, label_ids = self.compute_embeddings(
            self._dataset
        )
        self.add_to_index(embeddings, sample_ids, label_ids)
        self.find_duplicates(self._leak_threshold)
        self._cached_leaks_view = self.duplicates_view()
        return self._cached_leaks_view


###

### HASH BACKEND

_HASH_METHODS = ["filepath", "image"]


class LeakySplitsHashConfig(fob.BrainMethodConfig, LeakySplitsConfigInterface):
    """

    Args:
        hash_field (None): string, field to write hashes into
    """

    def __init__(
        self,
        split_views=None,
        split_field=None,
        split_tags=None,
        method="filepath",
        hash_field=None,
        **kwargs,
    ):
        self._method = method
        self.hash_field = hash_field
        LeakySplitsConfigInterface.__init__(
            self,
            split_views=split_views,
            split_field=split_field,
            split_tags=split_tags,
        )
        fob.BrainMethodConfig.__init__(self, **kwargs)

    @property
    def method(self):
        return self._method


class LeakySplitsHash(fob.BrainMethod):
    def initialize(self, samples, brain_key):
        return LeakySplitsHashIndex(
            samples, self.config, brain_key, backend=self
        )


class LeakySplitsHashIndex(fob.BrainResults, LeakySplitIndexInterface):
    """ """

    def __init__(self, samples, config, brain_key, backend):
        fob.BrainResults.__init__(
            self, samples, config, brain_key, backend=backend
        )
        LeakySplitIndexInterface.__init__(self)
        self._hash2ids = defaultdict(list)
        self.split_views = _to_views(
            samples,
            self.config.split_views,
            self.config.split_field,
            self.config.split_tags,
        )
        self._dataset = samples._dataset
        self._compute_hashes(samples)

    @property
    def _hash_function(self):
        if self.config.method == "filepath":
            return fou.compute_filehash

        elif self.config.method == "image":
            return LeakySplitsHashIndex._image_hash

    def _compute_hashes(self, samples):
        for s in samples.iter_samples():
            hash = str(self._hash_function(s["filepath"]))
            self._hash2ids[hash].append(s["id"])
            if self.config.hash_field:
                s[self.config.hash_field] = hash
                s.save()

    @staticmethod
    def _image_hash(image, hash_size=24):
        """
        Compute the dHash for the input image.

        :param image: image filepath
        :param hash_size: Size of the hash (default 8x8).
        :return: The dHash value of the image as a 64-bit integer.
        """

        with open(image, "r"):
            image = cv2.imread(image)

        # Convert the image to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Resize the image to (hash_size + 1, hash_size)
        resized = cv2.resize(gray, (hash_size + 1, hash_size))

        # Compute the differences between adjacent pixels
        diff = resized[:, 1:] > resized[:, :-1]

        # Convert the difference image to a binary hash
        # hash_value = sum([2 ** i for (i, v) in enumerate(diff.flatten()) if v])

        # Convert the difference image to a binary hash
        binary_string = "".join(["1" if v else "0" for v in diff.flatten()])

        # Convert the binary string to a hexadecimal string
        hex_hash = f"{int(binary_string, 2):0{hash_size * hash_size // 4}x}"

        return hex_hash

    @property
    def leaks(self):
        leak_ids = []
        for id_list in self._hash2ids.values():
            if len(id_list) > 1:
                leak_ids = leak_ids + id_list

        return self._dataset.select(leak_ids, ordered=True)

    def leaks_by_sample(self, sample):
        id = None
        if isinstance(sample, str):
            id = sample
        else:
            id = sample["id"]
        for id_list in self._hash2ids.values():
            if id in id_list:
                return self._dataset.select(id_list)


###
