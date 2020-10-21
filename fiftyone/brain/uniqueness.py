"""
Methods that compute insights related to sample uniqueness.

| Copyright 2017-2020, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import logging
import os

import numpy as np
from sklearn.neighbors import NearestNeighbors

import eta.core.learning as etal

import fiftyone.core.collections as foc
import fiftyone.core.labels as fol
import fiftyone.core.media as fom
import fiftyone.core.utils as fou


torch = fou.lazy_import("torch")
fout = fou.lazy_import("fiftyone.utils.torch")


logger = logging.getLogger(__name__)


_ALLOWED_ROI_FIELD_TYPES = (
    fol.Detection,
    fol.Detections,
    fol.Polyline,
    fol.Polylines,
)


def compute_uniqueness(samples, uniqueness_field="uniqueness", roi_field=None):
    """Adds a uniqueness field to each sample scoring how unique it is with
    respect to the rest of the samples.

    This function only uses the pixel data and can therefore process labeled or
    unlabeled samples.

    Args:
        samples: an iterable of :class:`fiftyone.core.sample.Sample` instances
        uniqueness_field ("uniqueness"): the field name to use to store the
            uniqueness value for each sample
        roi_field (None): an optional :class:`fiftyone.core.labels.Detection`,
            :class:`fiftyone.core.labels.Detections`,
            :class:`fiftyone.core.labels.Polyline`, or
            :class:`fiftyone.core.labels.Polylines` field defining a region of
            interest within each image to use to compute uniqueness
    """
    #
    # Algorithm
    #
    # Uniqueness is computed based on a classification model.  Each sample is
    # embedded into a vector space based on the model. Then, we compute the
    # knn's (k is a parameter of the uniqueness function). The uniqueness is
    # then proportional to these distances. The intuition is that a sample is
    # unique when it is far from other samples in the set. This is different
    # than, say, "representativeness" which would stress samples that are core
    # to dense clusters of related samples.
    #

    # Ensure that `torch` and `torchvision` are installed
    fou.ensure_torch()

    model = _load_model()

    if roi_field is None:
        embeddings = _compute_embeddings(samples, model)
    else:
        embeddings = _compute_patch_embeddings(samples, model, roi_field)

    uniqueness = _compute_uniqueness(embeddings)

    logger.info("Saving results...")
    with fou.ProgressBar() as pb:
        for sample, val in zip(pb(_optimize(samples)), uniqueness):
            sample[uniqueness_field] = val
            sample.save()

    logger.info("Uniqueness computation complete")


def _load_model():
    logger.info("Loading uniqueness model...")
    return etal.load_default_deployment_model("simple_resnet_cifar10")


def _compute_embeddings(samples, model):
    logger.info("Preparing data...")
    data_loader = _make_data_loader(samples, model.transforms)

    logger.info("Generating embeddings...")
    embeddings = None
    with fou.ProgressBar(samples) as pb:
        with torch.no_grad():
            for imgs in data_loader:
                # @todo the existence of model.embed_all is not well engineered
                vectors = model.embed_all(imgs)

                if embeddings is None:
                    embeddings = vectors
                else:
                    # @todo if speed is an issue, fix this...
                    embeddings = np.vstack((embeddings, vectors))

                pb.set_iteration(pb.iteration + len(imgs))

    # `num_samples x dim` array of embeddings
    return embeddings


def _compute_patch_embeddings(samples, model, roi_field):
    logger.info("Preparing data...")
    data_loader = _make_patch_data_loader(samples, model.transforms, roi_field)

    logger.info("Generating embeddings...")
    embeddings = None
    with fou.ProgressBar(samples) as pb:
        with torch.no_grad():
            for patches in pb(data_loader):
                # @todo the existence of model.embed_all is not well engineered
                patches = torch.squeeze(patches, dim=0)
                vectors = model.embed_all(patches)

                # Average over image patches
                embedding = vectors.mean(axis=0)

                if embeddings is None:
                    embeddings = embedding
                else:
                    # @todo if speed is an issue, fix this...
                    embeddings = np.vstack((embeddings, embedding))

    # `num_samples x dim` array of embeddings
    return embeddings


def _compute_uniqueness(embeddings):
    logger.info("Computing uniqueness...")

    # @todo convert to a parameter with a default, for tuning
    K = 3

    # First column of dists and indices is self-distance
    knns = NearestNeighbors(n_neighbors=K + 1, algorithm="ball_tree").fit(
        embeddings
    )
    dists, _ = knns.kneighbors(embeddings)

    #
    # @todo experiment on which method for assessing uniqueness is best
    #
    # To get something going, for now, just take a weighted mean
    #
    weights = [0.6, 0.3, 0.1]
    sample_dists = np.mean(dists[:, 1:] * weights, axis=1)

    # Normalize to keep the user on common footing across datasets
    sample_dists /= sample_dists.max()

    return sample_dists


def _make_data_loader(samples, transforms, batch_size=16):
    image_paths = []
    for sample in _optimize(samples):
        _validate(sample)
        image_paths.append(sample.filepath)

    dataset = fout.TorchImageDataset(
        image_paths, transform=transforms, force_rgb=True
    )

    return torch.utils.data.DataLoader(
        dataset, batch_size=batch_size, num_workers=4
    )


def _make_patch_data_loader(samples, transforms, roi_field):
    image_paths = []
    detections = []
    for sample in _optimize(samples, fields=[roi_field]):
        _validate(sample)
        rois = _parse_rois(sample, roi_field)
        if not rois.detections:
            # Use entire image as ROI
            rois = fol.Detections(
                detections=[fol.Detection(bounding_box=[0, 0, 1, 1])]
            )

        image_paths.append(sample.filepath)
        detections.append(rois)

    dataset = fout.TorchImagePatchesDataset(
        image_paths, detections, transforms, force_rgb=True
    )

    return torch.utils.data.DataLoader(dataset, batch_size=1, num_workers=4)


def _parse_rois(sample, roi_field):
    label = sample[roi_field]

    if isinstance(label, fol.Detections):
        return label

    if isinstance(label, fol.Detection):
        return fol.Detections(detections=[label])

    if isinstance(label, fol.Polyline):
        return fol.Detections(detections=[label.to_detection()])

    if isinstance(label, fol.Polylines):
        return label.to_detections()

    raise ValueError(
        "Sample '%s' field '%s' (%s) is not a valid ROI field; must be a %s "
        "instance"
        % (
            sample.id,
            roi_field,
            label.__class__.__name__,
            set(t.__name__ for t in _ALLOWED_ROI_FIELD_TYPES),
        )
    )


def _validate(sample):
    if not os.path.exists(sample.filepath):
        raise ValueError(
            "Sample '%s' source media '%s' does not exist on disk"
            % (sample.id, sample.filepath)
        )

    if sample.media_type != fom.IMAGE:
        raise ValueError(
            "Sample '%s' source media '%s' is not a recognized image format"
            % (sample.id, sample.filepath)
        )


def _optimize(samples, fields=None):
    # Selects only the requested fields (and always the default fields)
    if isinstance(samples, foc.SampleCollection):
        return samples.select_fields(fields)

    return samples
