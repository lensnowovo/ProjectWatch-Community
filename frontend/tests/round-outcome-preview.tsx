// Synthetic preview only. Never connects to the running instance.
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { MonitorLoop } from "../src/monitorLoop";
import { api, type MonitoringPlanView, type ProjectDetail } from "../src/api";
import "../src/styles.css";
api.signals = async () => ({ cadence: { hours: 12 } }) as never;
const waiting = new URLSearchParams(location.search).has("waiting");
const plan = {items:[{status:"active"},{status:"active"},{status:"active"}],changes:[],proposals:[],coverage:{company:[{state:"on"}]},state:{revisions:2},
  health:{hit_30d:2,items:3,system_added:1},
  last_round:{job_id:"synthetic-round",completed_at:new Date().toISOString(),coverage:"partial",assessed_events:2,
    evidence_only:1,object_changes:0,judgment_change:"unchanged",headline:"收费项目已明确，实际支付范围仍需核实。",
    stopped:1,active:0,waiting:0,standing:3,unfulfilled:0,pending:0,gaps:["地方支付原件尚未取得。","公司实际收费案例尚未确认。"],
    stop_reason:"本轮评估与修订已收口。已有结果保留，未完成项见缺口；不代表所有问题已解决。",
    stop_details:[{reason:"本次问题已回答",count:1}],
    outputs:{saved_entries:4,revision_applied:1,revision_added:0,revision_proposed:0,revision_dropped:0}}
} as unknown as MonitoringPlanView;
const detail = { project:{id:"synthetic",name:"合成科技",status:"active",activated:true,execution_mode:"api"},
  jobs:[],documents:[],counts:{company:12,policy:3},observations:{items:[]},event_states:{items:[]},
  monitoring_progress:{state:"observed",completed_at:new Date().toISOString(),round:2}, status_stale:waiting
} as unknown as ProjectDetail;
createRoot(document.getElementById("root")!).render(<BrowserRouter><main style={{maxWidth:1200,margin:"40px auto",padding:"0 24px"}}>
  <p className="muted small">合成数据 · 不连接当前实例</p><h1>合成科技</h1>
  <p className="muted">长期跟踪关联对象，有变化时展开研究。</p>
  <MonitorLoop detail={detail} plan={plan}/>
</main></BrowserRouter>);
