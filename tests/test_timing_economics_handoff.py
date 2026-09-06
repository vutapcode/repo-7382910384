import pytest
import time
from loi_he_thong import entry_lifecycle
from loi_he_thong import canonical_opportunity

class DummyState:
    pass

def test_timing_economics_handoff():
    s = DummyState()
    now = time.time()
    
    # 1. Start a wave
    result_1 = {
        'decision': 'GO',
        'causal_episode_id': 'wave_1',
        'side': 'LONG',
        'ignition': {
            'current_execution_proof': {
                'proof_hash': 'proof_1',
                'observed_at_ms': int(now*1000)
            },
            'clock_quality': {
                'a': {'epoch': 1}
            }
        }
    }
    
    # Observe canonical opportunity
    canonical_opportunity.observe(s, result_1, qualified=True, now=now)
    assert getattr(s, 'canonical_opportunity_active_episode_id', None) == 'wave_1'
    assert getattr(s, 'canonical_opportunity_active', False) is True
    
    # Observe lifecycle
    gate_1 = {'allowed': False, 'owner': 'TIMING', 'reason': 'WAIT_SPREAD'}
    lc_1 = entry_lifecycle.observe(s, result_1, gate_1, economic_opportunity_id=1)
    
    assert getattr(s, 'entry_timing_attempt_status', '') == 'WAIT'
    
    # 2. Attempt 1 expires
    gate_2 = {'allowed': False, 'owner': 'TIMING', 'reason': 'WAIT_STALE_COINBASE'}
    lc_2 = entry_lifecycle.observe(s, result_1, gate_2, economic_opportunity_id=1)
    
    assert getattr(s, 'entry_timing_attempt_status', '') == 'EXPIRED'
    assert getattr(s, 'canonical_opportunity_active', False) is True
    
    # 3. New GO with SAME wave, SAME proof (stale reuse) -> should be blocked
    allowed, reason = entry_lifecycle.can_create_attempt(s, result_1)
    assert not allowed
    assert reason == 'STALE_PROOF_REUSE'
    
    # 4. New GO with SAME wave, NEW proof -> should be allowed
    result_2 = dict(result_1)
    result_2['ignition'] = dict(result_1['ignition'])
    result_2['ignition']['current_execution_proof'] = {
        'proof_hash': 'proof_2',
        'observed_at_ms': int(now*1000) + 100
    }
    allowed, reason = entry_lifecycle.can_create_attempt(s, result_2)
    assert allowed
    
    # 5. New GO with SAME wave, NEW proof, DIFFERENT epoch -> blocked
    result_3 = dict(result_2)
    result_3['ignition'] = dict(result_2['ignition'])
    result_3['ignition']['clock_quality'] = {'a': {'epoch': 2}}
    allowed, reason = entry_lifecycle.can_create_attempt(s, result_3)
    assert not allowed
    assert reason == 'CAUSAL_EPOCH_CHANGED_WITHIN_WAVE'
    
    # 6. Parallel active attempt -> blocked
    gate_3 = {'allowed': False, 'owner': 'TIMING', 'reason': 'WAIT_SPREAD'}
    entry_lifecycle.observe(s, result_2, gate_3, economic_opportunity_id=2)
    assert getattr(s, 'entry_timing_attempt_status', '') == 'WAIT'
    
    result_4 = dict(result_2)
    result_4['ignition'] = dict(result_2['ignition'])
    result_4['ignition']['current_execution_proof'] = {
        'proof_hash': 'proof_3',
        'observed_at_ms': int(now*1000) + 200
    }
    allowed, reason = entry_lifecycle.can_create_attempt(s, result_4)
    assert not allowed
    assert reason == 'PARALLEL_ACTIVE_ATTEMPT'

