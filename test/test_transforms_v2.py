import itertools
import random

import numpy as np

import PIL.Image
import pytest
import torch
import torchvision.transforms.v2 as transforms

from common_utils import assert_equal, cpu_and_cuda
from torchvision import tv_tensors
from torchvision.ops.boxes import box_iou
from torchvision.transforms.functional import to_pil_image
from torchvision.transforms.v2._utils import is_pure_tensor
from transforms_v2_legacy_utils import make_bounding_boxes, make_detection_mask, make_image, make_images, make_videos


def make_vanilla_tensor_images(*args, **kwargs):
    for image in make_images(*args, **kwargs):
        if image.ndim > 3:
            continue
        yield image.data


def make_pil_images(*args, **kwargs):
    for image in make_vanilla_tensor_images(*args, **kwargs):
        yield to_pil_image(image)


def parametrize(transforms_with_inputs):
    return pytest.mark.parametrize(
        ("transform", "input"),
        [
            pytest.param(
                transform,
                input,
                id=f"{type(transform).__name__}-{type(input).__module__}.{type(input).__name__}-{idx}",
            )
            for transform, inputs in transforms_with_inputs
            for idx, input in enumerate(inputs)
        ],
    )


@pytest.mark.parametrize(
    "flat_inputs",
    itertools.permutations(
        [
            next(make_vanilla_tensor_images()),
            next(make_vanilla_tensor_images()),
            next(make_pil_images()),
            make_image(),
            next(make_videos()),
        ],
        3,
    ),
)
def test_pure_tensor_heuristic(flat_inputs):
    def split_on_pure_tensor(to_split):
        # This takes a sequence that is structurally aligned with `flat_inputs` and splits its items into three parts:
        # 1. The first pure tensor. If none is present, this will be `None`
        # 2. A list of the remaining pure tensors
        # 3. A list of all other items
        pure_tensors = []
        others = []
        # Splitting always happens on the original `flat_inputs` to avoid any erroneous type changes by the transform to
        # affect the splitting.
        for item, inpt in zip(to_split, flat_inputs):
            (pure_tensors if is_pure_tensor(inpt) else others).append(item)
        return pure_tensors[0] if pure_tensors else None, pure_tensors[1:], others

    class CopyCloneTransform(transforms.Transform):
        def _transform(self, inpt, params):
            return inpt.clone() if isinstance(inpt, torch.Tensor) else inpt.copy()

        @staticmethod
        def was_applied(output, inpt):
            identity = output is inpt
            if identity:
                return False

            # Make sure nothing fishy is going on
            assert_equal(output, inpt)
            return True

    first_pure_tensor_input, other_pure_tensor_inputs, other_inputs = split_on_pure_tensor(flat_inputs)

    transform = CopyCloneTransform()
    transformed_sample = transform(flat_inputs)

    first_pure_tensor_output, other_pure_tensor_outputs, other_outputs = split_on_pure_tensor(transformed_sample)

    if first_pure_tensor_input is not None:
        if other_inputs:
            assert not transform.was_applied(first_pure_tensor_output, first_pure_tensor_input)
        else:
            assert transform.was_applied(first_pure_tensor_output, first_pure_tensor_input)

    for output, inpt in zip(other_pure_tensor_outputs, other_pure_tensor_inputs):
        assert not transform.was_applied(output, inpt)

    for input, output in zip(other_inputs, other_outputs):
        assert transform.was_applied(output, input)


class TestTransform:
    @pytest.mark.parametrize(
        "inpt_type",
        [torch.Tensor, PIL.Image.Image, tv_tensors.Image, np.ndarray, tv_tensors.BoundingBoxes, str, int],
    )
    def test_check_transformed_types(self, inpt_type, mocker):
        # This test ensures that we correctly handle which types to transform and which to bypass
        t = transforms.Transform()
        inpt = mocker.MagicMock(spec=inpt_type)

        if inpt_type in (np.ndarray, str, int):
            output = t(inpt)
            assert output is inpt
        else:
            with pytest.raises(NotImplementedError):
                t(inpt)


class TestToImage:
    @pytest.mark.parametrize(
        "inpt_type",
        [torch.Tensor, PIL.Image.Image, tv_tensors.Image, np.ndarray, tv_tensors.BoundingBoxes, str, int],
    )
    def test__transform(self, inpt_type, mocker):
        fn = mocker.patch(
            "torchvision.transforms.v2.functional.to_image",
            return_value=torch.rand(1, 3, 8, 8),
        )

        inpt = mocker.MagicMock(spec=inpt_type)
        transform = transforms.ToImage()
        transform(inpt)
        if inpt_type in (tv_tensors.BoundingBoxes, tv_tensors.Image, str, int):
            assert fn.call_count == 0
        else:
            fn.assert_called_once_with(inpt)


class TestToPILImage:
    @pytest.mark.parametrize(
        "inpt_type",
        [torch.Tensor, PIL.Image.Image, tv_tensors.Image, np.ndarray, tv_tensors.BoundingBoxes, str, int],
    )
    def test__transform(self, inpt_type, mocker):
        fn = mocker.patch("torchvision.transforms.v2.functional.to_pil_image")

        inpt = mocker.MagicMock(spec=inpt_type)
        transform = transforms.ToPILImage()
        transform(inpt)
        if inpt_type in (PIL.Image.Image, tv_tensors.BoundingBoxes, str, int):
            assert fn.call_count == 0
        else:
            fn.assert_called_once_with(inpt, mode=transform.mode)


class TestToTensor:
    @pytest.mark.parametrize(
        "inpt_type",
        [torch.Tensor, PIL.Image.Image, tv_tensors.Image, np.ndarray, tv_tensors.BoundingBoxes, str, int],
    )
    def test__transform(self, inpt_type, mocker):
        fn = mocker.patch("torchvision.transforms.functional.to_tensor")

        inpt = mocker.MagicMock(spec=inpt_type)
        with pytest.warns(UserWarning, match="deprecated and will be removed"):
            transform = transforms.ToTensor()
        transform(inpt)
        if inpt_type in (tv_tensors.Image, torch.Tensor, tv_tensors.BoundingBoxes, str, int):
            assert fn.call_count == 0
        else:
            fn.assert_called_once_with(inpt)


class TestContainers:
    @pytest.mark.parametrize("transform_cls", [transforms.Compose, transforms.RandomChoice, transforms.RandomOrder])
    def test_assertions(self, transform_cls):
        with pytest.raises(TypeError, match="Argument transforms should be a sequence of callables"):
            transform_cls(transforms.RandomCrop(28))

    @pytest.mark.parametrize("transform_cls", [transforms.Compose, transforms.RandomChoice, transforms.RandomOrder])
    @pytest.mark.parametrize(
        "trfms",
        [
            [transforms.Pad(2), transforms.RandomCrop(28)],
            [lambda x: 2.0 * x, transforms.Pad(2), transforms.RandomCrop(28)],
            [transforms.Pad(2), lambda x: 2.0 * x, transforms.RandomCrop(28)],
        ],
    )
    def test_ctor(self, transform_cls, trfms):
        c = transform_cls(trfms)
        inpt = torch.rand(1, 3, 32, 32)
        output = c(inpt)
        assert isinstance(output, torch.Tensor)
        assert output.ndim == 4


class TestRandomChoice:
    def test_assertions(self):
        with pytest.raises(ValueError, match="Length of p doesn't match the number of transforms"):
            transforms.RandomChoice([transforms.Pad(2), transforms.RandomCrop(28)], p=[1])


class TestRandomIoUCrop:
    @pytest.mark.parametrize("device", cpu_and_cuda())
    @pytest.mark.parametrize("options", [[0.5, 0.9], [2.0]])
    def test__get_params(self, device, options):
        orig_h, orig_w = size = (24, 32)
        image = make_image(size)
        bboxes = tv_tensors.BoundingBoxes(
            torch.tensor([[1, 1, 10, 10], [20, 20, 23, 23], [1, 20, 10, 23], [20, 1, 23, 10]]),
            format="XYXY",
            canvas_size=size,
            device=device,
        )
        sample = [image, bboxes]

        transform = transforms.RandomIoUCrop(sampler_options=options)

        n_samples = 5
        for _ in range(n_samples):

            params = transform._get_params(sample)

            if options == [2.0]:
                assert len(params) == 0
                return

            assert len(params["is_within_crop_area"]) > 0
            assert params["is_within_crop_area"].dtype == torch.bool

            assert int(transform.min_scale * orig_h) <= params["height"] <= int(transform.max_scale * orig_h)
            assert int(transform.min_scale * orig_w) <= params["width"] <= int(transform.max_scale * orig_w)

            left, top = params["left"], params["top"]
            new_h, new_w = params["height"], params["width"]
            ious = box_iou(
                bboxes,
                torch.tensor([[left, top, left + new_w, top + new_h]], dtype=bboxes.dtype, device=bboxes.device),
            )
            assert ious.max() >= options[0] or ious.max() >= options[1], f"{ious} vs {options}"

    def test__transform_empty_params(self, mocker):
        transform = transforms.RandomIoUCrop(sampler_options=[2.0])
        image = tv_tensors.Image(torch.rand(1, 3, 4, 4))
        bboxes = tv_tensors.BoundingBoxes(torch.tensor([[1, 1, 2, 2]]), format="XYXY", canvas_size=(4, 4))
        label = torch.tensor([1])
        sample = [image, bboxes, label]
        # Let's mock transform._get_params to control the output:
        transform._get_params = mocker.MagicMock(return_value={})
        output = transform(sample)
        torch.testing.assert_close(output, sample)

    def test_forward_assertion(self):
        transform = transforms.RandomIoUCrop()
        with pytest.raises(
            TypeError,
            match="requires input sample to contain tensor or PIL images and bounding boxes",
        ):
            transform(torch.tensor(0))

    def test__transform(self, mocker):
        transform = transforms.RandomIoUCrop()

        size = (32, 24)
        image = make_image(size)
        bboxes = make_bounding_boxes(format="XYXY", canvas_size=size, batch_dims=(6,))
        masks = make_detection_mask(size, num_objects=6)

        sample = [image, bboxes, masks]

        is_within_crop_area = torch.tensor([0, 1, 0, 1, 0, 1], dtype=torch.bool)

        params = dict(top=1, left=2, height=12, width=12, is_within_crop_area=is_within_crop_area)
        transform._get_params = mocker.MagicMock(return_value=params)
        output = transform(sample)

        # check number of bboxes vs number of labels:
        output_bboxes = output[1]
        assert isinstance(output_bboxes, tv_tensors.BoundingBoxes)
        assert (output_bboxes[~is_within_crop_area] == 0).all()

        output_masks = output[2]
        assert isinstance(output_masks, tv_tensors.Mask)


class TestRandomShortestSize:
    @pytest.mark.parametrize("min_size,max_size", [([5, 9], 20), ([5, 9], None)])
    def test__get_params(self, min_size, max_size):
        canvas_size = (3, 10)

        transform = transforms.RandomShortestSize(min_size=min_size, max_size=max_size, antialias=True)

        sample = make_image(canvas_size)
        params = transform._get_params([sample])

        assert "size" in params
        size = params["size"]

        assert isinstance(size, tuple) and len(size) == 2

        longer = max(size)
        shorter = min(size)
        if max_size is not None:
            assert longer <= max_size
            assert shorter <= max_size
        else:
            assert shorter in min_size


class TestRandomResize:
    def test__get_params(self):
        min_size = 3
        max_size = 6

        transform = transforms.RandomResize(min_size=min_size, max_size=max_size, antialias=True)

        for _ in range(10):
            params = transform._get_params([])

            assert isinstance(params["size"], list) and len(params["size"]) == 1
            size = params["size"][0]

            assert min_size <= size < max_size


@pytest.mark.parametrize("image_type", (PIL.Image, torch.Tensor, tv_tensors.Image))
@pytest.mark.parametrize("label_type", (torch.Tensor, int))
@pytest.mark.parametrize("dataset_return_type", (dict, tuple))
@pytest.mark.parametrize("to_tensor", (transforms.ToTensor, transforms.ToImage))
def test_classif_preset(image_type, label_type, dataset_return_type, to_tensor):

    image = tv_tensors.Image(torch.randint(0, 256, size=(1, 3, 250, 250), dtype=torch.uint8))
    if image_type is PIL.Image:
        image = to_pil_image(image[0])
    elif image_type is torch.Tensor:
        image = image.as_subclass(torch.Tensor)
        assert is_pure_tensor(image)

    label = 1 if label_type is int else torch.tensor([1])

    if dataset_return_type is dict:
        sample = {
            "image": image,
            "label": label,
        }
    else:
        sample = image, label

    if to_tensor is transforms.ToTensor:
        with pytest.warns(UserWarning, match="deprecated and will be removed"):
            to_tensor = to_tensor()
    else:
        to_tensor = to_tensor()

    t = transforms.Compose(
        [
            transforms.RandomResizedCrop((224, 224), antialias=True),
            transforms.RandomHorizontalFlip(p=1),
            transforms.RandAugment(),
            transforms.TrivialAugmentWide(),
            transforms.AugMix(),
            transforms.AutoAugment(),
            to_tensor,
            # TODO: ConvertImageDtype is a pass-through on PIL images, is that
            # intended?  This results in a failure if we convert to tensor after
            # it, because the image would still be uint8 which make Normalize
            # fail.
            transforms.ConvertImageDtype(torch.float),
            transforms.Normalize(mean=[0, 0, 0], std=[1, 1, 1]),
            transforms.RandomErasing(p=1),
        ]
    )

    out = t(sample)

    assert type(out) == type(sample)

    if dataset_return_type is tuple:
        out_image, out_label = out
    else:
        assert out.keys() == sample.keys()
        out_image, out_label = out.values()

    assert out_image.shape[-2:] == (224, 224)
    assert out_label == label


@pytest.mark.parametrize("image_type", (PIL.Image, torch.Tensor, tv_tensors.Image))
@pytest.mark.parametrize("data_augmentation", ("hflip", "lsj", "multiscale", "ssd", "ssdlite"))
@pytest.mark.parametrize("to_tensor", (transforms.ToTensor, transforms.ToImage))
@pytest.mark.parametrize("sanitize", (True, False))
def test_detection_preset(image_type, data_augmentation, to_tensor, sanitize):
    torch.manual_seed(0)

    if to_tensor is transforms.ToTensor:
        with pytest.warns(UserWarning, match="deprecated and will be removed"):
            to_tensor = to_tensor()
    else:
        to_tensor = to_tensor()

    if data_augmentation == "hflip":
        t = [
            transforms.RandomHorizontalFlip(p=1),
            to_tensor,
            transforms.ConvertImageDtype(torch.float),
        ]
    elif data_augmentation == "lsj":
        t = [
            transforms.ScaleJitter(target_size=(1024, 1024), antialias=True),
            # Note: replaced FixedSizeCrop with RandomCrop, becuase we're
            # leaving FixedSizeCrop in prototype for now, and it expects Label
            # classes which we won't release yet.
            # transforms.FixedSizeCrop(
            #     size=(1024, 1024), fill=defaultdict(lambda: (123.0, 117.0, 104.0), {tv_tensors.Mask: 0})
            # ),
            transforms.RandomCrop((1024, 1024), pad_if_needed=True),
            transforms.RandomHorizontalFlip(p=1),
            to_tensor,
            transforms.ConvertImageDtype(torch.float),
        ]
    elif data_augmentation == "multiscale":
        t = [
            transforms.RandomShortestSize(
                min_size=(480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800), max_size=1333, antialias=True
            ),
            transforms.RandomHorizontalFlip(p=1),
            to_tensor,
            transforms.ConvertImageDtype(torch.float),
        ]
    elif data_augmentation == "ssd":
        t = [
            transforms.RandomPhotometricDistort(p=1),
            transforms.RandomZoomOut(fill={"others": (123.0, 117.0, 104.0), tv_tensors.Mask: 0}, p=1),
            transforms.RandomIoUCrop(),
            transforms.RandomHorizontalFlip(p=1),
            to_tensor,
            transforms.ConvertImageDtype(torch.float),
        ]
    elif data_augmentation == "ssdlite":
        t = [
            transforms.RandomIoUCrop(),
            transforms.RandomHorizontalFlip(p=1),
            to_tensor,
            transforms.ConvertImageDtype(torch.float),
        ]
    if sanitize:
        t += [transforms.SanitizeBoundingBoxes()]
    t = transforms.Compose(t)

    num_boxes = 5
    H = W = 250

    image = tv_tensors.Image(torch.randint(0, 256, size=(1, 3, H, W), dtype=torch.uint8))
    if image_type is PIL.Image:
        image = to_pil_image(image[0])
    elif image_type is torch.Tensor:
        image = image.as_subclass(torch.Tensor)
        assert is_pure_tensor(image)

    label = torch.randint(0, 10, size=(num_boxes,))

    boxes = torch.randint(0, min(H, W) // 2, size=(num_boxes, 4))
    boxes[:, 2:] += boxes[:, :2]
    boxes = boxes.clamp(min=0, max=min(H, W))
    boxes = tv_tensors.BoundingBoxes(boxes, format="XYXY", canvas_size=(H, W))

    masks = tv_tensors.Mask(torch.randint(0, 2, size=(num_boxes, H, W), dtype=torch.uint8))

    sample = {
        "image": image,
        "label": label,
        "boxes": boxes,
        "masks": masks,
    }

    out = t(sample)

    if isinstance(to_tensor, transforms.ToTensor) and image_type is not tv_tensors.Image:
        assert is_pure_tensor(out["image"])
    else:
        assert isinstance(out["image"], tv_tensors.Image)
    assert isinstance(out["label"], type(sample["label"]))

    num_boxes_expected = {
        # ssd and ssdlite contain RandomIoUCrop which may "remove" some bbox. It
        # doesn't remove them strictly speaking, it just marks some boxes as
        # degenerate and those boxes will be later removed by
        # SanitizeBoundingBoxes(), which we add to the pipelines if the sanitize
        # param is True.
        # Note that the values below are probably specific to the random seed
        # set above (which is fine).
        (True, "ssd"): 5,
        (True, "ssdlite"): 4,
    }.get((sanitize, data_augmentation), num_boxes)

    assert out["boxes"].shape[0] == out["masks"].shape[0] == out["label"].shape[0] == num_boxes_expected


@pytest.mark.parametrize("min_size", (1, 10))
@pytest.mark.parametrize("labels_getter", ("default", lambda inputs: inputs["labels"], None, lambda inputs: None))
@pytest.mark.parametrize("sample_type", (tuple, dict))
def test_sanitize_bounding_boxes(min_size, labels_getter, sample_type):

    if sample_type is tuple and not isinstance(labels_getter, str):
        # The "lambda inputs: inputs["labels"]" labels_getter used in this test
        # doesn't work if the input is a tuple.
        return

    H, W = 256, 128

    boxes_and_validity = [
        ([0, 1, 10, 1], False),  # Y1 == Y2
        ([0, 1, 0, 20], False),  # X1 == X2
        ([0, 0, min_size - 1, 10], False),  # H < min_size
        ([0, 0, 10, min_size - 1], False),  # W < min_size
        ([0, 0, 10, H + 1], False),  # Y2 > H
        ([0, 0, W + 1, 10], False),  # X2 > W
        ([-1, 1, 10, 20], False),  # any < 0
        ([0, 0, -1, 20], False),  # any < 0
        ([0, 0, -10, -1], False),  # any < 0
        ([0, 0, min_size, 10], True),  # H < min_size
        ([0, 0, 10, min_size], True),  # W < min_size
        ([0, 0, W, H], True),  # TODO: Is that actually OK?? Should it be -1?
        ([1, 1, 30, 20], True),
        ([0, 0, 10, 10], True),
        ([1, 1, 30, 20], True),
    ]

    random.shuffle(boxes_and_validity)  # For test robustness: mix order of wrong and correct cases
    boxes, is_valid_mask = zip(*boxes_and_validity)
    valid_indices = [i for (i, is_valid) in enumerate(is_valid_mask) if is_valid]

    boxes = torch.tensor(boxes)
    labels = torch.arange(boxes.shape[0])

    boxes = tv_tensors.BoundingBoxes(
        boxes,
        format=tv_tensors.BoundingBoxFormat.XYXY,
        canvas_size=(H, W),
    )

    masks = tv_tensors.Mask(torch.randint(0, 2, size=(boxes.shape[0], H, W)))
    whatever = torch.rand(10)
    input_img = torch.randint(0, 256, size=(1, 3, H, W), dtype=torch.uint8)
    sample = {
        "image": input_img,
        "labels": labels,
        "boxes": boxes,
        "whatever": whatever,
        "None": None,
        "masks": masks,
    }

    if sample_type is tuple:
        img = sample.pop("image")
        sample = (img, sample)

    out = transforms.SanitizeBoundingBoxes(min_size=min_size, labels_getter=labels_getter)(sample)

    if sample_type is tuple:
        out_image = out[0]
        out_labels = out[1]["labels"]
        out_boxes = out[1]["boxes"]
        out_masks = out[1]["masks"]
        out_whatever = out[1]["whatever"]
    else:
        out_image = out["image"]
        out_labels = out["labels"]
        out_boxes = out["boxes"]
        out_masks = out["masks"]
        out_whatever = out["whatever"]

    assert out_image is input_img
    assert out_whatever is whatever

    assert isinstance(out_boxes, tv_tensors.BoundingBoxes)
    assert isinstance(out_masks, tv_tensors.Mask)

    if labels_getter is None or (callable(labels_getter) and labels_getter({"labels": "blah"}) is None):
        assert out_labels is labels
    else:
        assert isinstance(out_labels, torch.Tensor)
        assert out_boxes.shape[0] == out_labels.shape[0] == out_masks.shape[0]
        # This works because we conveniently set labels to arange(num_boxes)
        assert out_labels.tolist() == valid_indices


def test_sanitize_bounding_boxes_no_label():
    # Non-regression test for https://github.com/pytorch/vision/issues/7878

    img = make_image()
    boxes = make_bounding_boxes()

    with pytest.raises(ValueError, match="or a two-tuple whose second item is a dict"):
        transforms.SanitizeBoundingBoxes()(img, boxes)

    out_img, out_boxes = transforms.SanitizeBoundingBoxes(labels_getter=None)(img, boxes)
    assert isinstance(out_img, tv_tensors.Image)
    assert isinstance(out_boxes, tv_tensors.BoundingBoxes)


def test_sanitize_bounding_boxes_errors():

    good_bbox = tv_tensors.BoundingBoxes(
        [[0, 0, 10, 10]],
        format=tv_tensors.BoundingBoxFormat.XYXY,
        canvas_size=(20, 20),
    )

    with pytest.raises(ValueError, match="min_size must be >= 1"):
        transforms.SanitizeBoundingBoxes(min_size=0)
    with pytest.raises(ValueError, match="labels_getter should either be 'default'"):
        transforms.SanitizeBoundingBoxes(labels_getter=12)

    with pytest.raises(ValueError, match="Could not infer where the labels are"):
        bad_labels_key = {"bbox": good_bbox, "BAD_KEY": torch.arange(good_bbox.shape[0])}
        transforms.SanitizeBoundingBoxes()(bad_labels_key)

    with pytest.raises(ValueError, match="must be a tensor"):
        not_a_tensor = {"bbox": good_bbox, "labels": torch.arange(good_bbox.shape[0]).tolist()}
        transforms.SanitizeBoundingBoxes()(not_a_tensor)

    with pytest.raises(ValueError, match="Number of boxes"):
        different_sizes = {"bbox": good_bbox, "labels": torch.arange(good_bbox.shape[0] + 3)}
        transforms.SanitizeBoundingBoxes()(different_sizes)


class TestLambda:
    inputs = pytest.mark.parametrize("input", [object(), torch.empty(()), np.empty(()), "string", 1, 0.0])

    @inputs
    def test_default(self, input):
        was_applied = False

        def was_applied_fn(input):
            nonlocal was_applied
            was_applied = True
            return input

        transform = transforms.Lambda(was_applied_fn)

        transform(input)

        assert was_applied

    @inputs
    def test_with_types(self, input):
        was_applied = False

        def was_applied_fn(input):
            nonlocal was_applied
            was_applied = True
            return input

        types = (torch.Tensor, np.ndarray)
        transform = transforms.Lambda(was_applied_fn, *types)

        transform(input)

        assert was_applied is isinstance(input, types)
