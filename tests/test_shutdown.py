import asyncio
import os
import signal

from everyframe_validator import runner


def test_sigterm_finishes_current_iteration_and_restores_handler(monkeypatch,tmp_path):
    events=[]
    previous=signal.getsignal(signal.SIGTERM)
    async def tick(*args,**kwargs):
        events.append('started')
        os.kill(os.getpid(),signal.SIGTERM)
        await asyncio.sleep(0.01)
        events.append('completed')
        return {'state':'dry_run'}
    monkeypatch.setattr(runner,'tick',tick)
    assert asyncio.run(asyncio.wait_for(runner.run(tmp_path,interval=60),1))==0
    assert events==['started','completed']
    assert signal.getsignal(signal.SIGTERM)==previous


def test_sigterm_interrupts_idle_wait(monkeypatch,tmp_path):
    calls=[]
    async def tick(*args,**kwargs):
        calls.append(1)
        asyncio.get_running_loop().call_later(0.01,os.kill,os.getpid(),signal.SIGTERM)
        return {'state':'dry_run'}
    monkeypatch.setattr(runner,'tick',tick)
    assert asyncio.run(asyncio.wait_for(runner.run(tmp_path,interval=60),1))==0
    assert len(calls)==1
