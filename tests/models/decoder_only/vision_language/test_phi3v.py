import os
import re
from typing import List, Optional, Tuple, Type

import pytest
from transformers import AutoTokenizer

from aphrodite.common.sequence import SampleLogprobs
from aphrodite.common.utils import is_cpu, is_hip
from aphrodite.multimodal.utils import rescale_image_size

from ....conftest import (IMAGE_ASSETS, AphroditeRunner, HfRunner,
                          PromptImageInput)
from ...utils import check_logprobs_close

HF_IMAGE_PROMPTS = IMAGE_ASSETS.prompts({
    "stop_sign":
    "<|user|>\n<|image_1|>\nWhat's the content of the image?<|end|>\n<|assistant|>\n",  # noqa: E501
    "cherry_blossom":
    "<|user|>\n<|image_1|>\nWhat is the season?<|end|>\n<|assistant|>\n",
})
HF_MULTIIMAGE_IMAGE_PROMPT = "<|user|>\n<|image_1|>\n<|image_2|>\nDescribe these images.<|end|>\n<|assistant|>\n"  # noqa: E501

models = ["microsoft/Phi-3.5-vision-instruct"]


def aphrodite_to_hf_output(aphrodite_output: Tuple[List[int], str,
                                         Optional[SampleLogprobs]],
                      model: str):
    """Sanitize aphrodite output to be comparable with hf output."""
    _, output_str, out_logprobs = aphrodite_output

    output_str_without_image = re.sub(r"(<\|image_\d+\|>)+", "", output_str)
    assert output_str_without_image[0] == " "
    output_str_without_image = output_str_without_image[1:]

    hf_output_str = output_str_without_image + "<|end|><|endoftext|>"

    tokenizer = AutoTokenizer.from_pretrained(model)
    hf_output_ids = tokenizer.encode(output_str_without_image)
    assert hf_output_ids[0] == 1
    hf_output_ids = hf_output_ids[1:]

    return hf_output_ids, hf_output_str, out_logprobs


target_dtype = "half"
if is_cpu():
    target_dtype = "bfloat16"

# ROCm Triton FA can run into shared memory issues with these models,
# use other backends in the meantime
# FIXME (mattwong, gshtrasb, hongxiayan)
if is_hip():
    os.environ["APHRODITE_USE_TRITON_FLASH_ATTN"] = "0"


def run_test(
    hf_runner: Type[HfRunner],
    aphrodite_runner: Type[AphroditeRunner],
    inputs: List[Tuple[List[str], PromptImageInput]],
    model: str,
    *,
    dtype: str,
    max_tokens: int,
    num_logprobs: int,
    mm_limit: int,
    tensor_parallel_size: int,
    distributed_executor_backend: Optional[str] = None,
):
    """Inference result should be the same between hf and aphrodite.

    All the image fixtures for the test are from IMAGE_ASSETS.
    For huggingface runner, we provide the PIL images as input.
    For aphrodite runner, we provide MultiModalDataDict objects 
    and corresponding MultiModalConfig as input.
    Note, the text input is also adjusted to abide by aphrodite contract.
    The text output is sanitized to be able to compare with hf.
    """

    # NOTE: take care of the order. run Aphrodite first, and then run HF.
    # Aphrodite needs a fresh new process without cuda initialization.
    # if we run HF first, the cuda initialization will be done and it
    # will hurt multiprocessing backend with fork method (the default method).

    # max_model_len should be greater than image_feature_size
    with aphrodite_runner(model,
                     max_model_len=4096,
                     max_num_seqs=1,
                     dtype=dtype,
                     limit_mm_per_prompt={"image": mm_limit},
                     tensor_parallel_size=tensor_parallel_size,
                     distributed_executor_backend=distributed_executor_backend,
                     enforce_eager=True) as aphrodite_model:
        aphrodite_outputs_per_case = [
            aphrodite_model.generate_greedy_logprobs(prompts,
                                                max_tokens,
                                                num_logprobs=num_logprobs,
                                                images=images)
            for prompts, images in inputs
        ]

    # use eager mode for hf runner, since phi3_v didn't work with flash_attn
    hf_model_kwargs = {"_attn_implementation": "eager"}
    with hf_runner(model, dtype=dtype,
                   model_kwargs=hf_model_kwargs) as hf_model:
        eos_token_id = hf_model.processor.tokenizer.eos_token_id
        hf_outputs_per_case = [
            hf_model.generate_greedy_logprobs_limit(prompts,
                                                    max_tokens,
                                                    num_logprobs=num_logprobs,
                                                    images=images,
                                                    eos_token_id=eos_token_id)
            for prompts, images in inputs
        ]

    for hf_outputs, aphrodite_outputs in zip(hf_outputs_per_case,
                                        aphrodite_outputs_per_case):
        check_logprobs_close(
            outputs_0_lst=hf_outputs,
            outputs_1_lst=[
                aphrodite_to_hf_output(aphrodite_output, model)
                for aphrodite_output in aphrodite_outputs
            ],
            name_0="hf",
            name_1="aphrodite",
        )


# Since we use _attn_implementation="eager" for hf_runner, there is more
# significant numerical difference. The basic `logprobs=5` fails to pass.
@pytest.mark.parametrize("model", models)
@pytest.mark.parametrize(
    "size_factors",
    [
        # No image
        [],
        # Single-scale
        [1.0],
        # Single-scale, batched
        [1.0, 1.0, 1.0],
        # Multi-scale
        [0.25, 0.5, 1.0],
    ],
)
@pytest.mark.parametrize("dtype", [target_dtype])
@pytest.mark.parametrize("max_tokens", [128])
@pytest.mark.parametrize("num_logprobs", [10])
def test_models(hf_runner, aphrodite_runner, image_assets, model, size_factors,
                dtype: str, max_tokens: int, num_logprobs: int) -> None:
    images = [asset.pil_image for asset in image_assets]

    inputs_per_image = [(
        [prompt for _ in size_factors],
        [rescale_image_size(image, factor) for factor in size_factors],
    ) for image, prompt in zip(images, HF_IMAGE_PROMPTS)]

    run_test(
        hf_runner,
        aphrodite_runner,
        inputs_per_image,
        model,
        dtype=dtype,
        max_tokens=max_tokens,
        num_logprobs=num_logprobs,
        mm_limit=1,
        tensor_parallel_size=1,
    )


@pytest.mark.parametrize("model", models)
@pytest.mark.parametrize("dtype", [target_dtype])
def test_regression_7840(hf_runner, aphrodite_runner, image_assets, model,
                         dtype) -> None:
    images = [asset.pil_image for asset in image_assets]

    inputs_regresion_7840 = [
        ([prompt], [image]) for image, prompt in zip(images, HF_IMAGE_PROMPTS)
    ]

    # Regression test for #7840.
    run_test(
        hf_runner,
        aphrodite_runner,
        inputs_regresion_7840,
        model,
        dtype=dtype,
        max_tokens=128,
        num_logprobs=10,
        mm_limit=1,
        tensor_parallel_size=1,
    )


@pytest.mark.parametrize("model", models)
@pytest.mark.parametrize(
    "size_factors",
    [
        # No image
        [],
        # Single-scale
        [1.0],
        # Single-scale, batched
        [1.0, 1.0, 1.0],
        # Multi-scale
        [0.25, 0.5, 1.0],
    ],
)
@pytest.mark.parametrize("dtype", [target_dtype])
@pytest.mark.parametrize("max_tokens", [128])
@pytest.mark.parametrize("num_logprobs", [10])
def test_multi_images_models(hf_runner, aphrodite_runner, image_assets, model,
                             size_factors, dtype: str, max_tokens: int,
                             num_logprobs: int) -> None:
    images = [asset.pil_image for asset in image_assets]

    inputs_per_case = [
        ([HF_MULTIIMAGE_IMAGE_PROMPT for _ in size_factors],
         [[rescale_image_size(image, factor) for image in images]
          for factor in size_factors])
    ]

    run_test(
        hf_runner,
        aphrodite_runner,
        inputs_per_case,
        model,
        dtype=dtype,
        max_tokens=max_tokens,
        num_logprobs=num_logprobs,
        mm_limit=2,
        tensor_parallel_size=1,
    )
