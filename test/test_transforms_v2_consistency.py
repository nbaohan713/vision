import importlib.machinery
import importlib.util
import inspect
import random
import re
from pathlib import Path

import numpy as np
import pytest

import torch
import torchvision.transforms.v2 as v2_transforms
from common_utils import assert_close, assert_equal, set_rng_seed
from torch import nn
from torchvision import transforms as legacy_transforms, tv_tensors
from torchvision._utils import sequence_to_str

from torchvision.transforms import functional as legacy_F
from torchvision.transforms.v2 import functional as prototype_F
from torchvision.transforms.v2._utils import _get_fill, query_size
from torchvision.transforms.v2.functional import to_pil_image
from transforms_v2_legacy_utils import (
    ArgsKwargs,
    make_bounding_boxes,
    make_detection_mask,
    make_image,
    make_images,
    make_segmentation_mask,
)

DEFAULT_MAKE_IMAGES_KWARGS = dict(color_spaces=["RGB"], extra_dims=[(4,)])


@pytest.fixture(autouse=True)
def fix_rng_seed():
    set_rng_seed(0)
    yield


class NotScriptableArgsKwargs(ArgsKwargs):
    """
    This class is used to mark parameters that render the transform non-scriptable. They still work in eager mode and
    thus will be tested there, but will be skipped by the JIT tests.
    """

    pass


class ConsistencyConfig:
    def __init__(
        self,
        prototype_cls,
        legacy_cls,
        # If no args_kwargs is passed, only the signature will be checked
        args_kwargs=(),
        make_images_kwargs=None,
        supports_pil=True,
        removed_params=(),
        closeness_kwargs=None,
    ):
        self.prototype_cls = prototype_cls
        self.legacy_cls = legacy_cls
        self.args_kwargs = args_kwargs
        self.make_images_kwargs = make_images_kwargs or DEFAULT_MAKE_IMAGES_KWARGS
        self.supports_pil = supports_pil
        self.removed_params = removed_params
        self.closeness_kwargs = closeness_kwargs or dict(rtol=0, atol=0)


# These are here since both the prototype and legacy transform need to be constructed with the same random parameters
LINEAR_TRANSFORMATION_MEAN = torch.rand(36)
LINEAR_TRANSFORMATION_MATRIX = torch.rand([LINEAR_TRANSFORMATION_MEAN.numel()] * 2)

CONSISTENCY_CONFIGS = [
    ConsistencyConfig(
        v2_transforms.ToPILImage,
        legacy_transforms.ToPILImage,
        [NotScriptableArgsKwargs()],
        make_images_kwargs=dict(
            color_spaces=[
                "GRAY",
                "GRAY_ALPHA",
                "RGB",
                "RGBA",
            ],
            extra_dims=[()],
        ),
        supports_pil=False,
    ),
    ConsistencyConfig(
        v2_transforms.Lambda,
        legacy_transforms.Lambda,
        [
            NotScriptableArgsKwargs(lambda image: image / 2),
        ],
        # Technically, this also supports PIL, but it is overkill to write a function here that supports tensor and PIL
        # images given that the transform does nothing but call it anyway.
        supports_pil=False,
    ),
    ConsistencyConfig(
        v2_transforms.PILToTensor,
        legacy_transforms.PILToTensor,
    ),
    ConsistencyConfig(
        v2_transforms.ToTensor,
        legacy_transforms.ToTensor,
    ),
    ConsistencyConfig(
        v2_transforms.Compose,
        legacy_transforms.Compose,
    ),
    ConsistencyConfig(
        v2_transforms.RandomApply,
        legacy_transforms.RandomApply,
    ),
    ConsistencyConfig(
        v2_transforms.RandomChoice,
        legacy_transforms.RandomChoice,
    ),
    ConsistencyConfig(
        v2_transforms.RandomOrder,
        legacy_transforms.RandomOrder,
    ),
]


@pytest.mark.parametrize("config", CONSISTENCY_CONFIGS, ids=lambda config: config.legacy_cls.__name__)
def test_signature_consistency(config):
    legacy_params = dict(inspect.signature(config.legacy_cls).parameters)
    prototype_params = dict(inspect.signature(config.prototype_cls).parameters)

    for param in config.removed_params:
        legacy_params.pop(param, None)

    missing = legacy_params.keys() - prototype_params.keys()
    if missing:
        raise AssertionError(
            f"The prototype transform does not support the parameters "
            f"{sequence_to_str(sorted(missing), separate_last='and ')}, but the legacy transform does. "
            f"If that is intentional, e.g. pending deprecation, please add the parameters to the `removed_params` on "
            f"the `ConsistencyConfig`."
        )

    extra = prototype_params.keys() - legacy_params.keys()
    extra_without_default = {
        param
        for param in extra
        if prototype_params[param].default is inspect.Parameter.empty
        and prototype_params[param].kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    }
    if extra_without_default:
        raise AssertionError(
            f"The prototype transform requires the parameters "
            f"{sequence_to_str(sorted(extra_without_default), separate_last='and ')}, but the legacy transform does "
            f"not. Please add a default value."
        )

    legacy_signature = list(legacy_params.keys())
    # Since we made sure that we don't have any extra parameters without default above, we clamp the prototype signature
    # to the same number of parameters as the legacy one
    prototype_signature = list(prototype_params.keys())[: len(legacy_signature)]

    assert prototype_signature == legacy_signature


def check_call_consistency(
    prototype_transform, legacy_transform, images=None, supports_pil=True, closeness_kwargs=None
):
    if images is None:
        images = make_images(**DEFAULT_MAKE_IMAGES_KWARGS)

    closeness_kwargs = closeness_kwargs or dict()

    for image in images:
        image_repr = f"[{tuple(image.shape)}, {str(image.dtype).rsplit('.')[-1]}]"

        image_tensor = torch.Tensor(image)
        try:
            torch.manual_seed(0)
            output_legacy_tensor = legacy_transform(image_tensor)
        except Exception as exc:
            raise pytest.UsageError(
                f"Transforming a tensor image {image_repr} failed in the legacy transform with the "
                f"error above. This means that you need to specify the parameters passed to `make_images` through the "
                "`make_images_kwargs` of the `ConsistencyConfig`."
            ) from exc

        try:
            torch.manual_seed(0)
            output_prototype_tensor = prototype_transform(image_tensor)
        except Exception as exc:
            raise AssertionError(
                f"Transforming a tensor image with shape {image_repr} failed in the prototype transform with "
                f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                f"`is_pure_tensor` path in `_transform`."
            ) from exc

        assert_close(
            output_prototype_tensor,
            output_legacy_tensor,
            msg=lambda msg: f"Tensor image consistency check failed with: \n\n{msg}",
            **closeness_kwargs,
        )

        try:
            torch.manual_seed(0)
            output_prototype_image = prototype_transform(image)
        except Exception as exc:
            raise AssertionError(
                f"Transforming a image tv_tensor with shape {image_repr} failed in the prototype transform with "
                f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                f"`tv_tensors.Image` path in `_transform`."
            ) from exc

        assert_close(
            output_prototype_image,
            output_prototype_tensor,
            msg=lambda msg: f"Output for tv_tensor and tensor images is not equal: \n\n{msg}",
            **closeness_kwargs,
        )

        if image.ndim == 3 and supports_pil:
            image_pil = to_pil_image(image)

            try:
                torch.manual_seed(0)
                output_legacy_pil = legacy_transform(image_pil)
            except Exception as exc:
                raise pytest.UsageError(
                    f"Transforming a PIL image with shape {image_repr} failed in the legacy transform with the "
                    f"error above. If this transform does not support PIL images, set `supports_pil=False` on the "
                    "`ConsistencyConfig`. "
                ) from exc

            try:
                torch.manual_seed(0)
                output_prototype_pil = prototype_transform(image_pil)
            except Exception as exc:
                raise AssertionError(
                    f"Transforming a PIL image with shape {image_repr} failed in the prototype transform with "
                    f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                    f"`PIL.Image.Image` path in `_transform`."
                ) from exc

            assert_close(
                output_prototype_pil,
                output_legacy_pil,
                msg=lambda msg: f"PIL image consistency check failed with: \n\n{msg}",
                **closeness_kwargs,
            )


@pytest.mark.parametrize(
    ("config", "args_kwargs"),
    [
        pytest.param(
            config, args_kwargs, id=f"{config.legacy_cls.__name__}-{idx:0{len(str(len(config.args_kwargs)))}d}"
        )
        for config in CONSISTENCY_CONFIGS
        for idx, args_kwargs in enumerate(config.args_kwargs)
    ],
)
@pytest.mark.filterwarnings("ignore")
def test_call_consistency(config, args_kwargs):
    args, kwargs = args_kwargs

    try:
        legacy_transform = config.legacy_cls(*args, **kwargs)
    except Exception as exc:
        raise pytest.UsageError(
            f"Initializing the legacy transform failed with the error above. "
            f"Please correct the `ArgsKwargs({args_kwargs})` in the `ConsistencyConfig`."
        ) from exc

    try:
        prototype_transform = config.prototype_cls(*args, **kwargs)
    except Exception as exc:
        raise AssertionError(
            "Initializing the prototype transform failed with the error above. "
            "This means there is a consistency bug in the constructor."
        ) from exc

    check_call_consistency(
        prototype_transform,
        legacy_transform,
        images=make_images(**config.make_images_kwargs),
        supports_pil=config.supports_pil,
        closeness_kwargs=config.closeness_kwargs,
    )


@pytest.mark.parametrize(
    ("config", "args_kwargs"),
    [
        pytest.param(
            config, args_kwargs, id=f"{config.legacy_cls.__name__}-{idx:0{len(str(len(config.args_kwargs)))}d}"
        )
        for config in CONSISTENCY_CONFIGS
        for idx, args_kwargs in enumerate(config.args_kwargs)
        if not isinstance(args_kwargs, NotScriptableArgsKwargs)
    ],
)
def test_jit_consistency(config, args_kwargs):
    args, kwargs = args_kwargs

    prototype_transform_eager = config.prototype_cls(*args, **kwargs)
    legacy_transform_eager = config.legacy_cls(*args, **kwargs)

    legacy_transform_scripted = torch.jit.script(legacy_transform_eager)
    prototype_transform_scripted = torch.jit.script(prototype_transform_eager)

    for image in make_images(**config.make_images_kwargs):
        image = image.as_subclass(torch.Tensor)

        torch.manual_seed(0)
        output_legacy_scripted = legacy_transform_scripted(image)

        torch.manual_seed(0)
        output_prototype_scripted = prototype_transform_scripted(image)

        assert_close(output_prototype_scripted, output_legacy_scripted, **config.closeness_kwargs)


class TestContainerTransforms:
    """
    Since we are testing containers here, we also need some transforms to wrap. Thus, testing a container transform for
    consistency automatically tests the wrapped transforms consistency.

    Instead of complicated mocking or creating custom transforms just for these tests, here we use deterministic ones
    that were already tested for consistency above.
    """

    def test_compose(self):
        prototype_transform = v2_transforms.Compose(
            [
                v2_transforms.Resize(256),
                v2_transforms.CenterCrop(224),
            ]
        )
        legacy_transform = legacy_transforms.Compose(
            [
                legacy_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ]
        )

        # atol=1 due to Resize v2 is using native uint8 interpolate path for bilinear and nearest modes
        check_call_consistency(prototype_transform, legacy_transform, closeness_kwargs=dict(rtol=0, atol=1))

    @pytest.mark.parametrize("p", [0, 0.1, 0.5, 0.9, 1])
    @pytest.mark.parametrize("sequence_type", [list, nn.ModuleList])
    def test_random_apply(self, p, sequence_type):
        prototype_transform = v2_transforms.RandomApply(
            sequence_type(
                [
                    v2_transforms.Resize(256),
                    v2_transforms.CenterCrop(224),
                ]
            ),
            p=p,
        )
        legacy_transform = legacy_transforms.RandomApply(
            sequence_type(
                [
                    legacy_transforms.Resize(256),
                    legacy_transforms.CenterCrop(224),
                ]
            ),
            p=p,
        )

        # atol=1 due to Resize v2 is using native uint8 interpolate path for bilinear and nearest modes
        check_call_consistency(prototype_transform, legacy_transform, closeness_kwargs=dict(rtol=0, atol=1))

        if sequence_type is nn.ModuleList:
            # quick and dirty test that it is jit-scriptable
            scripted = torch.jit.script(prototype_transform)
            scripted(torch.rand(1, 3, 300, 300))

    # We can't test other values for `p` since the random parameter generation is different
    @pytest.mark.parametrize("probabilities", [(0, 1), (1, 0)])
    def test_random_choice(self, probabilities):
        prototype_transform = v2_transforms.RandomChoice(
            [
                v2_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ],
            p=probabilities,
        )
        legacy_transform = legacy_transforms.RandomChoice(
            [
                legacy_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ],
            p=probabilities,
        )

        # atol=1 due to Resize v2 is using native uint8 interpolate path for bilinear and nearest modes
        check_call_consistency(prototype_transform, legacy_transform, closeness_kwargs=dict(rtol=0, atol=1))


class TestToTensorTransforms:
    def test_pil_to_tensor(self):
        prototype_transform = v2_transforms.PILToTensor()
        legacy_transform = legacy_transforms.PILToTensor()

        for image in make_images(extra_dims=[()]):
            image_pil = to_pil_image(image)

            assert_equal(prototype_transform(image_pil), legacy_transform(image_pil))

    def test_to_tensor(self):
        with pytest.warns(UserWarning, match=re.escape("The transform `ToTensor()` is deprecated")):
            prototype_transform = v2_transforms.ToTensor()
        legacy_transform = legacy_transforms.ToTensor()

        for image in make_images(extra_dims=[()]):
            image_pil = to_pil_image(image)
            image_numpy = np.array(image_pil)

            assert_equal(prototype_transform(image_pil), legacy_transform(image_pil))
            assert_equal(prototype_transform(image_numpy), legacy_transform(image_numpy))


def import_transforms_from_references(reference):
    HERE = Path(__file__).parent
    PROJECT_ROOT = HERE.parent

    loader = importlib.machinery.SourceFileLoader(
        "transforms", str(PROJECT_ROOT / "references" / reference / "transforms.py")
    )
    spec = importlib.util.spec_from_loader("transforms", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


det_transforms = import_transforms_from_references("detection")


class TestRefDetTransforms:
    def make_tv_tensors(self, with_mask=True):
        size = (600, 800)
        num_objects = 22

        def make_label(extra_dims, categories):
            return torch.randint(categories, extra_dims, dtype=torch.int64)

        pil_image = to_pil_image(make_image(size=size, color_space="RGB"))
        target = {
            "boxes": make_bounding_boxes(canvas_size=size, format="XYXY", batch_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (pil_image, target)

        tensor_image = torch.Tensor(make_image(size=size, color_space="RGB", dtype=torch.float32))
        target = {
            "boxes": make_bounding_boxes(canvas_size=size, format="XYXY", batch_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (tensor_image, target)

        tv_tensor_image = make_image(size=size, color_space="RGB", dtype=torch.float32)
        target = {
            "boxes": make_bounding_boxes(canvas_size=size, format="XYXY", batch_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (tv_tensor_image, target)

    @pytest.mark.parametrize(
        "t_ref, t, data_kwargs",
        [
            (det_transforms.RandomHorizontalFlip(p=1.0), v2_transforms.RandomHorizontalFlip(p=1.0), {}),
            (
                det_transforms.RandomIoUCrop(),
                v2_transforms.Compose(
                    [
                        v2_transforms.RandomIoUCrop(),
                        v2_transforms.SanitizeBoundingBoxes(labels_getter=lambda sample: sample[1]["labels"]),
                    ]
                ),
                {"with_mask": False},
            ),
            (det_transforms.RandomZoomOut(), v2_transforms.RandomZoomOut(), {"with_mask": False}),
            (det_transforms.ScaleJitter((1024, 1024)), v2_transforms.ScaleJitter((1024, 1024), antialias=True), {}),
            (
                det_transforms.RandomShortestSize(
                    min_size=(480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800), max_size=1333
                ),
                v2_transforms.RandomShortestSize(
                    min_size=(480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800), max_size=1333
                ),
                {},
            ),
        ],
    )
    def test_transform(self, t_ref, t, data_kwargs):
        for dp in self.make_tv_tensors(**data_kwargs):

            # We should use prototype transform first as reference transform performs inplace target update
            torch.manual_seed(12)
            output = t(dp)

            torch.manual_seed(12)
            expected_output = t_ref(*dp)

            assert_equal(expected_output, output)


seg_transforms = import_transforms_from_references("segmentation")


# We need this transform for two reasons:
# 1. transforms.RandomCrop uses a different scheme to pad images and masks of insufficient size than its name
#    counterpart in the detection references. Thus, we cannot use it with `pad_if_needed=True`
# 2. transforms.Pad only supports a fixed padding, but the segmentation datasets don't have a fixed image size.
class PadIfSmaller(v2_transforms.Transform):
    def __init__(self, size, fill=0):
        super().__init__()
        self.size = size
        self.fill = v2_transforms._geometry._setup_fill_arg(fill)

    def _get_params(self, sample):
        height, width = query_size(sample)
        padding = [0, 0, max(self.size - width, 0), max(self.size - height, 0)]
        needs_padding = any(padding)
        return dict(padding=padding, needs_padding=needs_padding)

    def _transform(self, inpt, params):
        if not params["needs_padding"]:
            return inpt

        fill = _get_fill(self.fill, type(inpt))
        return prototype_F.pad(inpt, padding=params["padding"], fill=fill)


class TestRefSegTransforms:
    def make_tv_tensors(self, supports_pil=True, image_dtype=torch.uint8):
        size = (256, 460)
        num_categories = 21

        conv_fns = []
        if supports_pil:
            conv_fns.append(to_pil_image)
        conv_fns.extend([torch.Tensor, lambda x: x])

        for conv_fn in conv_fns:
            tv_tensor_image = make_image(size=size, color_space="RGB", dtype=image_dtype)
            tv_tensor_mask = make_segmentation_mask(size=size, num_categories=num_categories, dtype=torch.uint8)

            dp = (conv_fn(tv_tensor_image), tv_tensor_mask)
            dp_ref = (
                to_pil_image(tv_tensor_image) if supports_pil else tv_tensor_image.as_subclass(torch.Tensor),
                to_pil_image(tv_tensor_mask),
            )

            yield dp, dp_ref

    def set_seed(self, seed=12):
        torch.manual_seed(seed)
        random.seed(seed)

    def check(self, t, t_ref, data_kwargs=None):
        for dp, dp_ref in self.make_tv_tensors(**data_kwargs or dict()):

            self.set_seed()
            actual = actual_image, actual_mask = t(dp)

            self.set_seed()
            expected_image, expected_mask = t_ref(*dp_ref)
            if isinstance(actual_image, torch.Tensor) and not isinstance(expected_image, torch.Tensor):
                expected_image = legacy_F.pil_to_tensor(expected_image)
            expected_mask = legacy_F.pil_to_tensor(expected_mask).squeeze(0)
            expected = (expected_image, expected_mask)

            assert_equal(actual, expected)

    @pytest.mark.parametrize(
        ("t_ref", "t", "data_kwargs"),
        [
            (
                seg_transforms.RandomHorizontalFlip(flip_prob=1.0),
                v2_transforms.RandomHorizontalFlip(p=1.0),
                dict(),
            ),
            (
                seg_transforms.RandomHorizontalFlip(flip_prob=0.0),
                v2_transforms.RandomHorizontalFlip(p=0.0),
                dict(),
            ),
            (
                seg_transforms.RandomCrop(size=480),
                v2_transforms.Compose(
                    [
                        PadIfSmaller(size=480, fill={tv_tensors.Mask: 255, "others": 0}),
                        v2_transforms.RandomCrop(size=480),
                    ]
                ),
                dict(),
            ),
            (
                seg_transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                v2_transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                dict(supports_pil=False, image_dtype=torch.float),
            ),
        ],
    )
    def test_common(self, t_ref, t, data_kwargs):
        self.check(t, t_ref, data_kwargs)


@pytest.mark.parametrize(
    ("legacy_dispatcher", "name_only_params"),
    [
        (legacy_F.get_dimensions, {}),
        (legacy_F.get_image_size, {}),
        (legacy_F.get_image_num_channels, {}),
        (legacy_F.to_tensor, {}),
        (legacy_F.pil_to_tensor, {}),
        (legacy_F.convert_image_dtype, {}),
        (legacy_F.to_pil_image, {}),
        (legacy_F.to_grayscale, {}),
        (legacy_F.rgb_to_grayscale, {}),
        (legacy_F.to_tensor, {}),
    ],
)
def test_dispatcher_signature_consistency(legacy_dispatcher, name_only_params):
    legacy_signature = inspect.signature(legacy_dispatcher)
    legacy_params = list(legacy_signature.parameters.values())[1:]

    try:
        prototype_dispatcher = getattr(prototype_F, legacy_dispatcher.__name__)
    except AttributeError:
        raise AssertionError(
            f"Legacy dispatcher `F.{legacy_dispatcher.__name__}` has no prototype equivalent"
        ) from None

    prototype_signature = inspect.signature(prototype_dispatcher)
    prototype_params = list(prototype_signature.parameters.values())[1:]

    # Some dispatchers got extra parameters. This makes sure they have a default argument and thus are BC. We don't
    # need to check if parameters were added in the middle rather than at the end, since that will be caught by the
    # regular check below.
    prototype_params, new_prototype_params = (
        prototype_params[: len(legacy_params)],
        prototype_params[len(legacy_params) :],
    )
    for param in new_prototype_params:
        assert param.default is not param.empty

    # Some annotations were changed mostly to supersets of what was there before. Plus, some legacy dispatchers had no
    # annotations. In these cases we simply drop the annotation and default argument from the comparison
    for prototype_param, legacy_param in zip(prototype_params, legacy_params):
        if legacy_param.name in name_only_params:
            prototype_param._annotation = prototype_param._default = inspect.Parameter.empty
            legacy_param._annotation = legacy_param._default = inspect.Parameter.empty
        elif legacy_param.annotation is inspect.Parameter.empty:
            prototype_param._annotation = inspect.Parameter.empty

    assert prototype_params == legacy_params
