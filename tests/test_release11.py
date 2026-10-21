import unittest
from unittest.mock import Mock
from cbus_bridge.utils import RampTracker,BridgeDiagnostics
from cbus_bridge import main


class ThreeSecondReadbackTests(unittest.TestCase):
    def make(self):
        now=[0.];d=BridgeDiagnostics({})
        t=RampTracker(d,{(56,13),(56,14),(56,40)},interval=3,clock=lambda:now[0])
        return t,now,d

    def begin(self,t,g=13):
        token=t.begin(56,g,0,60);t.ack(token,True)

    def test_three_second_ramp_updates_without_idle_polling(self):
        t,now,d=self.make();self.begin(t)
        sends=[]
        for at in range(70):
            now[0]=at;token=t.reserve()
            if token and t.send(token,lambda app,block:sends.append((at,block))):
                t.observe(56,13,max(0,255-round(at*255/60)))
        self.assertEqual(sends[:4],[(1,0),(4,0),(7,0),(10,0)])
        self.assertTrue(all(b[0]-a[0]>=3 for a,b in zip(sends,sends[1:])))
        self.assertEqual(t.snapshot()['active_groups'],0)
        self.assertEqual(t.snapshot()['global_min_interval_s'],3)
        self.assertEqual(d.snapshot()['counters']['ramp_tracking_completed_total'],1)
        now[0]=100;self.assertIsNone(t.reserve())

    def test_multiple_blocks_share_global_budget_and_same_block_coalesces(self):
        t,now,d=self.make()
        for g in (13,14,40):self.begin(t,g)
        frames=[]
        for at in range(1,14):
            now[0]=at;token=t.reserve()
            if token and t.send(token,lambda app,block:frames.append((at,block))):
                for g in (13,14) if token['block'][1]==0 else (40,):t.observe(56,g,100)
        self.assertEqual(frames,[(1,0),(4,32),(7,0),(10,32),(13,0)])

    def test_missing_replies_remain_bounded_at_faster_rate(self):
        t,now,d=self.make();self.begin(t)
        sent=[]
        for at in range(30):
            now[0]=at;token=t.reserve()
            if token:t.send(token,lambda *args:sent.append(at))
        self.assertEqual(sent,[1,4,7])
        self.assertEqual(t.snapshot()['groups'][0]['phase'],'readback_failed')
        self.assertEqual(d.snapshot()['counters']['ramp_readback_reply_timeouts_total'],3)

    def test_cli_accepts_new_range_and_launcher_defaults_to_three(self):
        from pathlib import Path
        for seconds in ('0','3','9','10','60'):
            args=main.build_arg_parser().parse_args(['--cni-host','unused','--ha-config','unused','--ramp-readback-every',seconds])
            self.assertEqual(args.ramp_readback_every,float(seconds))
        script=(Path(__file__).resolve().parents[1]/'run_bridge_service.sh').read_text()
        self.assertIn('${RAMP_READBACK_EVERY:-3}',script)
