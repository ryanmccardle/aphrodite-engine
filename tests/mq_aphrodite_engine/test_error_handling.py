"""Test that various errors are handled properly."""

import asyncio
import tempfile
import time
import uuid
from unittest.mock import Mock

import pytest

from aphrodite import SamplingParams
from aphrodite.common.utils import FlexibleArgumentParser
from aphrodite.endpoints.openai.api_server import build_engine_client
from aphrodite.endpoints.openai.args import make_arg_parser
from aphrodite.engine.aphrodite_engine import AphroditeEngine
from aphrodite.engine.args_tools import AsyncEngineArgs
from aphrodite.engine.multiprocessing import MQEngineDeadError
from aphrodite.engine.multiprocessing.engine import MQAphroditeEngine
from aphrodite.lora.request import LoRARequest
from tests.mq_aphrodite_engine.utils import RemoteMQAphroditeEngine

MODEL = "google/gemma-1.1-2b-it"
ENGINE_ARGS = AsyncEngineArgs(model=MODEL)
RAISED_ERROR = KeyError
RAISED_VALUE = "foo"


@pytest.fixture(scope="function")
def tmp_socket():
    with tempfile.TemporaryDirectory() as td:
        yield f"ipc://{td}/{uuid.uuid4()}"


def run_with_evil_forward(engine_args: AsyncEngineArgs, ipc_path: str):
    # Make engine.
    engine = MQAphroditeEngine.from_engine_args(
        engine_args=engine_args,
        ipc_path=ipc_path)

    # Raise error during first forward pass.
    engine.engine.model_executor.execute_model = Mock(
        side_effect=RAISED_ERROR(RAISED_VALUE))

    # Run engine.
    engine.start()


@pytest.mark.asyncio
async def test_evil_forward(tmp_socket):
    with RemoteMQAphroditeEngine(engine_args=ENGINE_ARGS,
                           ipc_path=tmp_socket,
                           run_fn=run_with_evil_forward) as engine:

        client = await engine.make_client()

        # Server should be healthy after initial probe.
        await asyncio.sleep(2.0)
        await client.check_health()

        # Throws an error in first forward pass.
        with pytest.raises(RAISED_ERROR):
            async for _ in client.generate(prompt="Hello my name is",
                                           sampling_params=SamplingParams(),
                                           request_id=uuid.uuid4()):
                pass
        assert client.errored

        # Engine is errored, should get ENGINE_DEAD_ERROR.
        with pytest.raises(MQEngineDeadError):
            async for _ in client.generate(prompt="Hello my name is",
                                           sampling_params=SamplingParams(),
                                           request_id=uuid.uuid4()):
                pass
        assert client.errored

        await asyncio.sleep(1.0)
        with pytest.raises(RAISED_ERROR):
            await client.check_health()
        assert client.errored

        # Shutdown.
        client.close()


def run_with_evil_model_executor_health(engine_args: AsyncEngineArgs,
                                        ipc_path: str):
    # Make engine.
    engine = MQAphroditeEngine.from_engine_args(
        engine_args=engine_args,
        ipc_path=ipc_path)

    # Raise error during first forward pass.
    engine.engine.model_executor.check_health = Mock(side_effect=RAISED_ERROR)

    # Run engine.
    engine.start()


@pytest.mark.asyncio
async def test_failed_health_check(tmp_socket):
    with RemoteMQAphroditeEngine(
            engine_args=ENGINE_ARGS,
            ipc_path=tmp_socket,
            run_fn=run_with_evil_model_executor_health) as engine:

        client = await engine.make_client()
        assert client.is_running

        # Health probe should throw RAISED_ERROR.
        await asyncio.sleep(15.)

        with pytest.raises(RAISED_ERROR):
            await client.check_health()
        assert client.errored

        # Generate call should throw ENGINE_DEAD_ERROR
        with pytest.raises(MQEngineDeadError):
            async for _ in client.generate(prompt="Hello my name is",
                                           sampling_params=SamplingParams(),
                                           request_id=uuid.uuid4()):
                pass

        client.close()


def run_with_evil_abort(engine_args: AsyncEngineArgs, ipc_path: str):
    # Make engine.
    engine = MQAphroditeEngine.from_engine_args(
        engine_args=engine_args,
        ipc_path=ipc_path)

    # Raise error during abort call.
    engine.engine.abort_request = Mock(side_effect=RAISED_ERROR)

    # Run engine.
    engine.start()


@pytest.mark.asyncio
async def test_failed_abort(tmp_socket):
    with RemoteMQAphroditeEngine(engine_args=ENGINE_ARGS,
                           ipc_path=tmp_socket,
                           run_fn=run_with_evil_abort) as engine:

        client = await engine.make_client()
        assert client.is_running

        # Firsh check health should work.
        await client.check_health()

        # Trigger an abort on the client side.
        async def bad_abort_after_2s():
            await asyncio.sleep(2.0)
            await client.abort(request_id="foo")

        # Trigger an abort in 2s from now.
        abort_task = asyncio.create_task(bad_abort_after_2s())

        # Exception in abort() will happen during this generation.
        # This will kill the engine and should return ENGINE_DEAD_ERROR
        # with reference to the original KeyError("foo")
        with pytest.raises(MQEngineDeadError) as execinfo:
            async for _ in client.generate(
                    prompt="Hello my name is",
                    sampling_params=SamplingParams(max_tokens=2000),
                    request_id=uuid.uuid4()):
                pass
        assert "KeyError" in repr(execinfo.value)
        assert client.errored

        await abort_task

        # This should raise the original error.
        with pytest.raises(RAISED_ERROR):
            await client.check_health()

        client.close()


@pytest.mark.asyncio
async def test_bad_request(tmp_socket):
    with RemoteMQAphroditeEngine(engine_args=ENGINE_ARGS,
                           ipc_path=tmp_socket) as engine:

        client = await engine.make_client()

        # Invalid request should fail, but not crash the server.
        with pytest.raises(ValueError):
            async for _ in client.generate(prompt="Hello my name is",
                                           sampling_params=SamplingParams(),
                                           request_id="abcd-1",
                                           lora_request=LoRARequest(
                                               "invalid-lora", 1,
                                               "invalid-path")):
                pass

        # This request should be okay.
        async for _ in client.generate(prompt="Hello my name is",
                                       sampling_params=SamplingParams(),
                                       request_id="abcd-2"):
            pass

        # Shutdown.
        client.close()


@pytest.mark.asyncio
async def test_mp_crash_detection(monkeypatch):

    parser = FlexibleArgumentParser(
        description="Aphrodite's remote OpenAI server.")
    parser = make_arg_parser(parser)
    args = parser.parse_args([])

    # When AphroditeEngine is loaded, it will crash.
    def mock_init():
        raise ValueError

    monkeypatch.setattr(AphroditeEngine, "__init__", mock_init)

    start = time.perf_counter()
    async with build_engine_client(args):
        pass
    end = time.perf_counter()

    assert end - start < 60, (
        "Expected Aphrodite to gracefully shutdown in <60s "
        "if there is an error in the startup.")


@pytest.mark.asyncio
async def test_mp_cuda_init():
    # it should not crash, when cuda is initialized
    # in the API server process
    import torch
    torch.cuda.init()
    parser = FlexibleArgumentParser(
        description="Aphrodite's remote OpenAI server.")
    parser = make_arg_parser(parser)
    args = parser.parse_args([])

    async with build_engine_client(args):
        pass
