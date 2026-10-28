"""Bounded retries for read-only inference, never motor commands or whole steps."""
import asyncio
import time

from .ai_provider import AIProviderError


RETRYABLE=frozenset({'timeout','connection_failed','upstream_error','rate_limited',
    'invalid_response','incomplete_response','invalid_structured_output'})


def _category(error):
    if isinstance(error,AIProviderError):return error.category
    if isinstance(error,(ValueError,TypeError)):return 'invalid_structured_output'
    if isinstance(error,TimeoutError):return 'timeout'
    return None


async def verification_retry(operation,authorized,*,max_attempts=3,timeout_s=120,
        token_budget=1600,max_token_budget=3200,on_attempt=None,backoff=.2):
    """Call operation(output_token_budget) over the same observations at most 3x.

    Caller owns schema validation inside operation and must supply a live lease/
    epoch check. A cancelled or no-longer-authorized result is discarded, even
    when an uncooperative provider returns after cancellation. No retry callback
    receives exception text, provider payloads, images, requests or credentials.
    """
    if type(max_attempts) is not int or not 1<=max_attempts<=3:raise ValueError('Invalid retry count')
    if not 0<timeout_s<=120 or not 0<=backoff<=2:raise ValueError('Invalid retry time bound')
    if type(token_budget) is not int or type(max_token_budget) is not int or not 128<=token_budget<=max_token_budget<=4096:
        raise ValueError('Invalid token budget')
    deadline=time.monotonic()+timeout_s
    budget=token_budget
    def check():
        if not authorized():raise asyncio.CancelledError()
    def report(attempt,category,started):
        if on_attempt:
            try:on_attempt(attempt=attempt,error_category=category,latency_ms=round((time.monotonic()-started)*1000),token_budget=budget)
            except Exception:pass
    for attempt in range(1,max_attempts+1):
        check()
        started=time.monotonic()
        if started>=deadline:raise AIProviderError('timeout',504)
        task=asyncio.create_task(operation(budget))
        try:
            while not task.done():
                check()
                remaining=deadline-time.monotonic()
                if remaining<=0:raise AIProviderError('timeout',504)
                await asyncio.wait({task},timeout=min(.1,remaining))
            check()
            value=task.result()
            check()
            report(attempt,None,started)
            return value
        except Exception as error:
            category=_category(error)
            report(attempt,category if category in RETRYABLE else 'permanent_error',started)
            filtered=isinstance(error,AIProviderError) and getattr(error,'incomplete_reason',None)=='content_filter'
            if category not in RETRYABLE or filtered or attempt==max_attempts or time.monotonic()>=deadline:raise
            if category=='incomplete_response' and getattr(error,'incomplete_reason',None)=='max_output_tokens':
                budget=min(max_token_budget,budget+800)
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        until=min(deadline,time.monotonic()+backoff*attempt)
        while time.monotonic()<until:
            check()
            await asyncio.sleep(min(.05,until-time.monotonic()))
