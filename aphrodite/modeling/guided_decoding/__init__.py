from typing import Optional, Union

from aphrodite.common.sampling_params import LogitsProcessorFunc
from aphrodite.endpoints.openai.protocol import (
    ChatCompletionNamedToolChoiceParam, ChatCompletionRequest,
    CompletionRequest)
from aphrodite.modeling.guided_decoding.guided_fields import (
    GuidedDecodingRequest)


async def get_guided_decoding_logits_processor(
        guided_decoding_backend: str, request: Union[CompletionRequest,
                                                     ChatCompletionRequest],
        tokenizer) -> Optional[LogitsProcessorFunc]:
    request = _adapt_request_for_tool_use(request)
    if guided_decoding_backend == 'outlines':
        from aphrodite.modeling.guided_decoding.outlines_decoding import (
            get_outlines_guided_decoding_logits_processor)
        return await get_outlines_guided_decoding_logits_processor(
            request, tokenizer)
    if guided_decoding_backend == 'lm-format-enforcer':
            pass
    if guided_decoding_backend == 'lm-format-enforcer':
        from aphrodite.modeling.guided_decoding.lm_format_enforcer_decoding import (  # noqa
            get_lm_format_enforcer_guided_decoding_logits_processor)
        return await get_lm_format_enforcer_guided_decoding_logits_processor(
            request, tokenizer)

    raise ValueError(
        f"Unknown guided decoding backend '{guided_decoding_backend}'. "
        "Must be one of 'outlines, 'lm-format-enforcer'")


def get_local_guided_decoding_logits_processor(
        guided_decoding_backend: str, guided_options: GuidedDecodingRequest,
        tokenizer) -> Optional[LogitsProcessorFunc]:
    # request = _adapt_request_for_tool_use(request)

    if guided_decoding_backend == 'outlines':
        from aphrodite.modeling.guided_decoding.outlines_decoding import (
            get_local_outlines_guided_decoding_logits_processor)
        return get_local_outlines_guided_decoding_logits_processor(
            guided_options, tokenizer)
    if guided_decoding_backend == 'lm-format-enforcer':
        from aphrodite.modeling.guided_decoding.lm_format_enforcer_decoding import (  # noqa
            get_local_lm_format_enforcer_guided_decoding_logits_processor)
        return get_local_lm_format_enforcer_guided_decoding_logits_processor(
            guided_options, tokenizer)

    raise ValueError(
        f"Unknown guided decoding backend '{guided_decoding_backend}'. "
        "Must be one of 'outlines, 'lm-format-enforcer'")


def _adapt_request_for_tool_use(request: Union[CompletionRequest,
                                               ChatCompletionRequest]):
    # the legacy completion API does not support tool use
    if type(request) is CompletionRequest:
        return request

    # user has chosen to not use any tool,
    # OR is allowing the model to choose a tool.
    if request.tool_choice == "none" or request.tool_choice == "auto":
        return request

    # user has chosen to use a named tool
    if type(request.tool_choice) is ChatCompletionNamedToolChoiceParam:
        tool_name = request.tool_choice.function.name
        tools = {tool.function.name: tool.function for tool in request.tools}
        if tool_name not in tools:
            raise ValueError(
                f"Tool '{tool_name}' has not been passed in `tools`.")
        tool = tools[tool_name]
        request.guided_json = tool.parameters

    return request
