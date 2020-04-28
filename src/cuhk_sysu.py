import logging
import os
import os.path as osp

import numpy as np
from detectron2.data import DatasetCatalog, MetadataCatalog
from detectron2.structures import BoxMode
from PIL import Image
from scipy.io import loadmat

from .utils import pickle, unpickle


class CUHK_SYSU:
    def __init__(self, dirname, split):
        self.dirname = dirname
        self.split = split
        self.data_path = osp.join(self.dirname, "Image", "SSM")
        self.classes = ["background", "person"]
        self.logger = logging.getLogger(__name__)
        self.image_indexes = self.load_image_indexes()
        self.roidb = self.load_roidb()
        if split == "test":
            self.probes = self.load_probes()

    @property
    def num_images(self):
        return len(self.image_indexes)

    def image_path_at(self, i):
        image_path = osp.join(self.data_path, self.image_indexes[i])
        assert osp.isfile(image_path), "Path does not exist: %s" % image_path
        return image_path

    def load_image_indexes(self):
        """
        Load the image indexes for training / testing.
        """
        # Test images
        test = loadmat(osp.join(self.dirname, "annotation", "pool.mat"))
        test = test["pool"].squeeze()
        test = [str(a[0]) for a in test]
        if self.split == "test":
            return test

        # All images
        all_imgs = loadmat(osp.join(self.dirname, "annotation", "Images.mat"))
        all_imgs = all_imgs["Img"].squeeze()
        all_imgs = [str(a[0][0]) for a in all_imgs]

        # Training images = all images - test images
        train = list(set(all_imgs) - set(test))
        train.sort()
        return train

    def load_probes(self):
        """
        Load the list of (img, roi) for probes.
        """
        protocol = loadmat(osp.join(self.dirname, "annotation/test/train_test/TestG50.mat"))
        protocol = protocol["TestG50"].squeeze()
        probes = []
        for item in protocol["Query"]:
            im_name = osp.join(self.data_path, str(item["imname"][0, 0][0]))
            roi = item["idlocate"][0, 0][0].astype(np.int32)
            roi[2:] += roi[:2]
            probes.append((im_name, roi))
        return probes

    def load_roidb(self):
        """
        Load the ground-truth roidb for each image.

        The roidb of each image is a dictionary that has the following keys:
            gt_boxes (ndarray[N, 4]): all ground-truth boxes in (x1, y1, x2, y2) format
            gt_pids (ndarray[N]): person IDs for these ground-truth boxes
            image (str): image path
            width (int): image width
            height (int): image height
        """
        cache_path = osp.join(self.dirname, "cache")
        if not osp.exists(cache_path):
            os.makedirs(cache_path)
        cache_file = osp.join(cache_path, self.split + "_roidb.pkl")
        if osp.isfile(cache_file):
            return unpickle(cache_file)

        # Load all images and build a dict from image to boxes
        all_imgs = loadmat(osp.join(self.dirname, "annotation", "Images.mat"))
        all_imgs = all_imgs["Img"].squeeze()
        name_to_boxes = {}
        name_to_pids = {}
        for im_name, _, boxes in all_imgs:
            im_name = str(im_name[0])
            boxes = np.asarray([b[0] for b in boxes[0]])
            boxes = boxes.reshape(boxes.shape[0], 4)
            valid_index = np.where((boxes[:, 2] > 0) & (boxes[:, 3] > 0))[0]
            assert valid_index.size > 0, "Warning: %s has no valid boxes." % im_name
            boxes = boxes[valid_index]
            name_to_boxes[im_name] = boxes.astype(np.int32)
            name_to_pids[im_name] = -1 * np.ones(boxes.shape[0], dtype=np.int32)

        def set_box_pid(boxes, box, pids, pid):
            for i in range(boxes.shape[0]):
                if np.all(boxes[i] == box):
                    pids[i] = pid
                    return
            self.logger.warning("Person: %s, box: %s cannot find in images." % (pid, box))

        # Load all the train / test persons and label their pids from 0 to N - 1
        # Assign pid = -1 for unlabeled background people
        if self.split == "train":
            train = loadmat(osp.join(self.dirname, "annotation/test/train_test/Train.mat"))
            train = train["Train"].squeeze()
            for index, item in enumerate(train):
                scenes = item[0, 0][2].squeeze()
                for im_name, box, _ in scenes:
                    im_name = str(im_name[0])
                    box = box.squeeze().astype(np.int32)
                    set_box_pid(name_to_boxes[im_name], box, name_to_pids[im_name], index)
        else:
            test = loadmat(osp.join(self.dirname, "annotation/test/train_test/TestG50.mat"))
            test = test["TestG50"].squeeze()
            for index, item in enumerate(test):
                # query
                im_name = str(item["Query"][0, 0][0][0])
                box = item["Query"][0, 0][1].squeeze().astype(np.int32)
                set_box_pid(name_to_boxes[im_name], box, name_to_pids[im_name], index)

                # gallery
                gallery = item["Gallery"].squeeze()
                for im_name, box, _ in gallery:
                    im_name = str(im_name[0])
                    if box.size == 0:
                        break
                    box = box.squeeze().astype(np.int32)
                    set_box_pid(name_to_boxes[im_name], box, name_to_pids[im_name], index)

        # Construct the roidb
        roidb = []
        for i, im_name in enumerate(self.image_indexes):
            boxes = name_to_boxes[im_name]
            boxes[:, 2] += boxes[:, 0]
            boxes[:, 3] += boxes[:, 1]
            pids = name_to_pids[im_name]
            size = Image.open(self.image_path_at(i)).size
            roidb.append(
                {
                    "gt_boxes": boxes,
                    "gt_pids": pids,
                    "image": self.image_path_at(i),
                    "height": size[1],
                    "width": size[0],
                }
            )
        pickle(roidb, cache_file)
        self.logger.info("Save ground-truth roidb to: %s" % cache_file)
        return roidb


def load_cuhk_sysu_instances(dirname, split):
    """
    Load the instances of images.

    Each gallery image has the following properties:
        file_name, image_id, height, width, annotations

    Each probe image only has two properties: file_name, probe

    Args:
        dirname (str): directory of the dataset
        split (str): "train" or "test"
    """
    dataset = CUHK_SYSU(dirname, split)
    dicts = []

    # gallery images
    for i, image_index in enumerate(dataset.image_indexes):
        roidb = dataset.roidb[i]
        dict = {}
        dict["file_name"] = roidb["image"]
        dict["image_id"] = image_index
        dict["height"] = roidb["height"]
        dict["width"] = roidb["width"]
        instances = []
        for gt_box, gt_pid in zip(roidb["gt_boxes"], roidb["gt_pids"]):
            instances.append(
                {
                    "bbox": gt_box,
                    "bbox_mode": BoxMode.XYXY_ABS,
                    "category_id": 1,
                    "person_id": gt_pid,
                }
            )
        dict["annotations"] = instances
        dicts.append(dict)

    # probe images
    # if split == "test":
    #     for probe in dataset.probes:
    #         dict = {}
    #         dict["file_name"] = probe[0]
    #         dict["probe"] = probe[1]
    #         dicts.append(dict)

    return dicts


def register_cuhk_sysu(dirname):
    for dataset, split in zip(["cuhk_sysu_train", "cuhk_sysu_test"], ["train", "test"]):
        DatasetCatalog.register(dataset, lambda: load_cuhk_sysu_instances(dirname, split))
        MetadataCatalog.get(dataset).set(
            thing_classes=["background", "person"], dirname=dirname, evaluator_type="cuhk_sysu"
        )


register_cuhk_sysu("./data")
