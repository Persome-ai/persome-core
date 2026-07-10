"""The paper runtime's local 3D personal-model view.

Adapted from ``docs/superpowers/mockups/2026-07-02-memory-ontology-three.html``
— the 定稿模型 canvas is the ACCEPTANCE AUTHORITY. The visual language is the
spec §7-6 COMPLETE axis→channel classification (one channel per state axis,
Cartesian product covered, no overloads, no gaps):

- **points** — kind → azimuth SECTOR (person/org/project/artifact; events use
  the bottom height band, self at origin) · 时态 → HEIGHT BAND (live mid /
  historical sunk / event-terminal bottom ring) · consolidation → RADIUS
  (strong=near USER) + size · connectivity → TEXTURE (solid disc vs hollow
  dashed orphan ring) · lens → COLOR only (种类/validity/记忆度). Positions
  are a pure function of identity + now-state: the as-of scrubber changes
  visibility/color, never the layout.
- **edges** — observations → thickness (always) · historical → gray-thin
  (masks lens color) · status → opacity (shadow constantly dim; labels only
  for active or obs≥3) · 作用面/方向/极性 → the three edge lenses.
- **面** — color = per-face IDENTITY hue (provenance rides LINE STYLE:
  both=solid bright frame+fill / single=dashed weak, exactly the mockup);
  vertices = the anchor entities it emerged from, NEVER including USER.
  Complete n-case dispatch: n≥3 hull · n=2 translucent spindle · n=1 halo
  ring on the anchor · n=0 tower plate.
- **体** — per-body identity hue, always-solid frame; vertices = member
  anchors ∪ USER (§1.5-3 rollup). m≥2 hull · m=1 USER↔anchor spindle ·
  m=0 plate.
- **time scrubber + ▶** — client-side f(T) over the REAL bitemporal fields;
  edges carry evidence-time ``valid_from`` so the axis has real depth.

Served by ``GET /model``. All JavaScript modules and model data are loaded from
the same loopback server, so the page remains functional without network access.
"""

MEMORY_VIEW_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Persome Memory — 记忆图（真库）</title>
<style>
  :root{--bg:#0c0e13;--panel:#171a21;--ink:#e8ebf0;--dim:#98a0ad;--line:#2a2f3a;--user:#c65cff;--fork:#e06666;--live:#3fb970;}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;overflow:hidden}
  .wrap{display:flex;height:100%}
  .canvas{position:relative;flex:1;overflow:hidden}
  #view{position:absolute;inset:0;background:radial-gradient(ellipse 90% 75% at 50% 32%,#191c24 0%,#101218 45%,#0a0b0f 100%)}
  .toolbar{position:absolute;top:12px;left:12px;z-index:8;display:flex;gap:6px;flex-wrap:wrap;align-items:center;max-width:70%}
  .toolbar span.lbl{color:var(--dim);font-size:11px}
  .toolbar button{background:rgba(23,26,33,.9);color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:4px 8px;font-size:12px;cursor:pointer}
  .toolbar button.on{border-color:var(--user);color:#fff}
  .timebar{position:absolute;bottom:12px;left:50%;transform:translateX(-50%);z-index:8;display:flex;align-items:center;gap:10px;background:rgba(23,26,33,.95);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
  .timebar input[type=range]{width:300px;accent-color:var(--user)}
  .timebar #tlabel{font-size:12px;min-width:96px}
  .timebar button{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 9px;cursor:pointer}
  .legend{position:absolute;top:52px;right:12px;z-index:7;background:rgba(23,26,33,.92);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:12px;max-width:255px;max-height:70vh;overflow:auto}
  .legend .row{display:flex;align-items:center;gap:7px;margin:3px 0;color:var(--dim)}
  .legend .sec{margin-top:7px;color:var(--ink);font-weight:600;font-size:11px}
  .dot{width:11px;height:11px;border-radius:50%;flex:none}
  .bar{width:20px;height:0;border-top:3px solid;flex:none}
  .stats{position:absolute;top:12px;right:12px;z-index:7;background:rgba(23,26,33,.92);border:1px solid var(--line);border-radius:10px;padding:7px 12px;font-size:12px;color:var(--dim);max-width:255px}
  .stats b{color:var(--ink)}
  .detail{position:absolute;bottom:64px;left:12px;z-index:8;background:rgba(23,26,33,.95);border:1px solid var(--line);border-radius:10px;padding:10px 13px;font-size:12.5px;color:var(--dim);max-width:360px;max-height:52vh;overflow:auto;display:none}
  .detail h3{margin:0 0 5px;font-size:13px;color:var(--user)}
  .detail b{color:var(--ink)}
  .lbl3d{font-size:11px;font-weight:500;letter-spacing:.2px;color:rgba(236,239,245,.94);text-shadow:0 1px 3px rgba(0,0,0,.92);pointer-events:none;white-space:nowrap;background:rgba(10,12,17,.5);padding:1px 6px;border-radius:6px;transition:opacity .18s ease}
  .lbl3d.sm{font-size:9.5px;color:rgba(203,210,223,.86);background:rgba(10,12,17,.42)}
  .lbl3d.ghost{color:rgba(167,175,191,.68);font-style:italic;background:rgba(10,12,17,.3)}
  .lbl3d.hide{display:none}
  .lbl3d.hl{background:rgba(42,33,62,.92);box-shadow:0 0 0 1px var(--user);color:#fff;z-index:20}
  #err{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--fork);padding:40px;text-align:center;white-space:pre-wrap;z-index:9}
  @media(max-width:700px){
    .toolbar{top:8px;left:8px;right:8px;max-width:none;gap:4px}
    .toolbar span.lbl{display:none}
    .toolbar button{padding:4px 6px;font-size:11px}
    .stats{top:88px;left:8px;right:8px;max-width:none;font-size:10px;padding:6px 8px}
    .legend{display:none}
    .timebar{left:8px;right:8px;bottom:8px;transform:none;gap:6px;padding:7px 9px}
    .timebar input[type=range]{width:auto;min-width:0;flex:1}
    .detail{left:8px;right:8px;bottom:60px;max-width:none}
  }
</style>
<script type="importmap">
{"imports":{
  "three":"/model/assets/three.module.js",
  "three/addons/":"/model/assets/jsm/"
}}
</script>
</head>
<body>
<div class="wrap">
  <div class="canvas">
    <div class="toolbar">
      <span class="lbl">点:</span>
      <button data-lens="kind" class="on">种类</button><button data-lens="validity">validity</button><button data-lens="mem">记忆度</button>
      <span class="lbl">| 边:</span>
      <button data-elens="mod" class="on">作用面</button><button data-elens="dir">方向</button><button data-elens="val">极性</button>
      <span class="lbl">|</span><button id="schemaBtn" class="on">▦ 面</button><button id="bodyBtn" class="on">▦▦ 体</button><button id="spinBtn">⟳</button>
    </div>
    <div id="view"></div><div id="err"></div>
    <div class="stats" id="stats">加载中…</div>
    <div class="legend" id="legend"></div>
    <div class="detail" id="detail"></div>
    <div class="timebar"><button id="play">▶</button><span class="lbl" style="color:var(--dim);font-size:11px">as-of</span>
      <input type="range" id="time" min="0" max="24" step="1" value="24"><span id="tlabel">now</span></div>
  </div>
</div>

<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {CSS2DRenderer, CSS2DObject} from 'three/addons/renderers/CSS2DRenderer.js';
import {ConvexGeometry} from 'three/addons/geometries/ConvexGeometry.js';
const errBox=document.getElementById('err');
window.addEventListener('error',e=>{errBox.style.display='flex';errBox.textContent='加载/渲染出错。\n'+e.message;});

// ── 真数据（可重取——实时轮询长大，延时摄影用）──────────────────────
let nodes=[], edges=[], faces=[], searchState={}, modelSnapshot={points:[]}, byId={}, semGeo={facts:[],edges:[],faces:[]};
function ingest(data){
  nodes=data.nodes||[]; edges=data.edges||[]; faces=data.faces||[]; semGeo=data.sem_geo||{facts:[],edges:[],faces:[]};
  searchState=data.search||{}; modelSnapshot=data.model||{points:[]}; byId=Object.fromEntries(nodes.map(n=>[n.id,n]));
}
// ── 统一语义 fact-空间（用户 2026-07-07 拍板的模型 · Y=涌现层级）──────────────
// 最小单元=fact（和力导向图里同一种单元，只是换了坐标域）。坐标：XZ 平面=语义(embedding)；
// Y 轴=涌现层级（点/fact 在语义地板 Y=0 → 面/Schema 浮在成员簇正上方 → 体在更高处）。时间不占
// 空间轴，改由底部 as-of 拖动条驱动（拖它=按沉积时间淡入 fact，延时摄影）。fact 间连接=k-NN 语义
// 相似边；连接之上涌现：面=成员事实卷成的华盖（伞骨=成员→面节点），体=面的面（更高一层）。
const SPLANE=5.2, FACE_Y=1.9, BODY_Y=3.3; // 语义半宽；面/体的涌现高度
let semFPos=[]; // 每个 fact 的地板位置（下标对齐 semGeo.facts）
function curFrac(){ // as-of 拖动条 → 时间分数（now=1）；fact.y∈[0,1] 是归一化沉积时间
  try{ if(typeof slider!=='undefined' && +slider.value<STEPS) return +slider.value/STEPS; }catch(e){}
  return 1;
}
function renderSemSpace(){
  const frac=curFrac();
  const vis=semGeo.facts.map(f=>(f.y==null?0:f.y)<=frac+1e-6); // 该 fact 到 as-of 时刻是否已沉积
  semFPos=semGeo.facts.map(f=>new THREE.Vector3(f.x*SPLANE, 0, f.z*SPLANE)); // 语义地板，Y=0
  // ① fact-fact 连接（k-NN 语义相似边，地板上）
  for(const [a,b,w] of (semGeo.edges||[])){if(!vis[a]||!vis[b])continue;const A=semFPos[a],B=semFPos[b];if(!A||!B)continue;
    graph.add(edgeMesh(A,B,new THREE.Color(0x5a8fb0),0.004+(w-0.5)*0.01,0.06+(w-0.5)*0.4));}
  // ② 涌现：面 = 一个空间簇（k-NN 图社区）。每个 fact 按所属面着色（簇=彩色区域，直接看得见）；
  //    面本身画成「成员事实的凸包轮廓」（只取外边界 → 无尖刺）浮在簇上方 + 一条主干连回簇质心（上下
  //    链接）。按成员数错开高度 → 越大的面升得越高 = 点→面(→体) 的纵向层级。凸包只描边+极淡填充，
  //    不糊成墙。fact→面 着色表先建好，供 ③ 用。
  const factHue={}; // fact 下标 → 所属面的色相
  const vfaces=[];
  for(const fc of (semGeo.faces||[])){const isBody=fc.level>=2;
    const mem=(fc.members||[]).filter(i=>semFPos[i]&&vis[i]);if(mem.length<3)continue;
    const hue=hash(fc.id); (fc.members||[]).forEach(i=>{factHue[i]=hue;});
    if((fc.level===1&&!showSchema)||(fc.level===2&&!showBody))continue;
    const cen=mem.reduce((s,i)=>s.add(semFPos[i].clone()),new THREE.Vector3()).multiplyScalar(1/mem.length);
    vfaces.push({fc,isBody,mem,cen,hue});}
  vfaces.sort((a,b)=>a.mem.length-b.mem.length); // 小的在下、大的在上
  vfaces.forEach((vf,k)=>{const{fc,isBody,mem,cen,hue}=vf;const col=new THREE.Color().setHSL(hue,0.6,0.62);
    const H=(isBody?BODY_Y:FACE_Y)+(isBody?0:k*0.24); const apex=new THREE.Vector3(cen.x,H,cen.z);
    const hull=hull2d(mem.map(i=>({x:semFPos[i].x,z:semFPos[i].z}))); // 只取外边界 → 无尖刺凸多边形
    if(hull.length>=3){const hc={x:hull.reduce((s,p)=>s+p.x,0)/hull.length,z:hull.reduce((s,p)=>s+p.z,0)/hull.length};
      const pos=[];for(let j=0;j<hull.length;j++){const b=hull[(j+1)%hull.length];pos.push(hc.x,H,hc.z, hull[j].x,H,hull[j].z, b.x,H,b.z);}
      const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));geo.computeVertexNormals();
      const fill=new THREE.Mesh(geo,new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:isBody?0.08:0.05,side:THREE.DoubleSide,depthWrite:false}));
      fill.userData={kind:'face',id:fc.id};graph.add(fill);(isBody?bodyMeshes:faceMeshes).push(fill);
      const lp=hull.map(r=>new THREE.Vector3(r.x,H,r.z));lp.push(lp[0]);
      graph.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(lp),new THREE.LineBasicMaterial({color:col,transparent:true,opacity:0.6})));}
    graph.add(edgeMesh(new THREE.Vector3(cen.x,0,cen.z),apex,col,0.01,0.45)); // 主干：地板质心→面（上下链接）
    const fn=new THREE.Sprite(new THREE.SpriteMaterial({map:circleTex,color:col,transparent:true,opacity:0.95,depthWrite:false}));
    fn.scale.setScalar(isBody?0.32:0.18);fn.position.copy(apex);fn.renderOrder=3;fn.userData={kind:'face',id:fc.id};graph.add(fn);(isBody?bodyMeshes:faceMeshes).push(fn);
    const lab=mkLabel(faceMark(fc.level)+(fc.sig||'').replace(/^用户/,'').slice(0,14)+'·'+mem.length,'sm','#'+col.getHexString(),44,1,'涌现自 '+mem.length+' 个事实（空间簇）：'+(fc.sig||''),{kind:'face',id:fc.id});
    lab.position.copy(apex.clone().add(new THREE.Vector3(0,0.2,0)));graph.add(lab);});
  // ③ 每个 fact 一个节点（地板；色相=所属面/社区，亮度/大小 ∝ 连接度=重要度；悬停→详情）
  const deg=new Array(semGeo.facts.length).fill(0);
  for(const [a,b] of (semGeo.edges||[])){if(deg[a]!=null)deg[a]++;if(deg[b]!=null)deg[b]++;}
  const maxDeg=Math.max(1,...deg);
  for(let i=0;i<semGeo.facts.length;i++){if(!vis[i])continue;const pos=semFPos[i];const d=deg[i]/maxDeg;
    const op=0.42+d*0.55, sz=0.08+d*0.24;
    const hue=(factHue[i]==null?0.55:factHue[i]), sat=(factHue[i]==null?0.12:0.62);
    const col=new THREE.Color().setHSL(hue,sat,0.46+d*0.32);
    let g=null; const gSz=0.3+d*1.15, gOp=d*0.5;
    if(d>0.22){g=new THREE.Sprite(new THREE.SpriteMaterial({map:glowTex,color:col,transparent:true,opacity:gOp,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending}));
      g.scale.setScalar(gSz);g.position.copy(pos);g.renderOrder=1;g.userData={glow:true,baseScale:gSz,baseOp:gOp};graph.add(g);}
    const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:circleTex,color:col,transparent:true,opacity:op,depthWrite:false}));
    sp.scale.setScalar(sz);sp.position.copy(pos);sp.renderOrder=2;sp.userData={kind:'fact',fi:i,baseOp:op,baseScale:sz,target:pos.clone(),glow:g};graph.add(sp);nodeSprites.push(sp);
    if(d>0.55){const t=semGeo.facts[i].t||'';const lab=mkLabel(t.slice(0,16),'sm ghost','#'+col.getHexString(),50+Math.round(d*20),1,t,{kind:'fact',id:i});
      lab.position.copy(pos.clone().add(new THREE.Vector3(0,sz+0.12,0)));graph.add(lab);}}
}
// 2D 凸包（Andrew monotone chain）：只保留外边界顶点 → 面 surface 无内部尖刺
function hull2d(pts){if(pts.length<3)return pts.slice();
  const p=pts.slice().sort((a,b)=>a.x-b.x||a.z-b.z),cr=(o,a,b)=>(a.x-o.x)*(b.z-o.z)-(a.z-o.z)*(b.x-o.x);
  const lo=[];for(const q of p){while(lo.length>=2&&cr(lo[lo.length-2],lo[lo.length-1],q)<=0)lo.pop();lo.push(q);}
  const up=[];for(let i=p.length-1;i>=0;i--){const q=p[i];while(up.length>=2&&cr(up[up.length-2],up[up.length-1],q)<=0)up.pop();up.push(q);}
  lo.pop();up.pop();return lo.concat(up);}
ingest(await (await fetch('/model/graph')).json());

// ── 轴 ↔ 通道（spec §7-6 完备分类）────────────────────────────────
const kcls=n=>n.kind==='self'?'user':(n.kind==='event'?'occ':'cont');
const KIND={user:0xc65cff,cont:0x3aa0a0,occ:0xd9a441};
const PFAM={engaged_with:'floor',participates_in:'dyn',part_of:'struct',reports_to:'struct',knows:'aff',about:'sem',depends_on:'struct'};
const FAM={struct:0x5b8dff,dyn:0xe0803c,sem:0x46c68a,aff:0xb57cff,floor:0x3a3f4a};
const VAL={'+':0x3fb970,'-':0xe06666,'0':0x6b7280};
const SYM=new Set(['knows']);
// 面/体身份色：id 的确定性色相（provenance 走线型，不占颜色通道——mockup 语义）
function hash(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return (h>>>0)/4294967296;}
const idColor=id=>new THREE.Color().setHSL(hash(id),0.55,0.58);
let lens='kind',eLens='mod',showSchema=true,showBody=true,focus=null;

// ── 布局：位置 = 身份 + now 态的纯函数（as-of 只改可见性/颜色）──────
// θ 扇区=kind；y 带=时态；r=证据强度（近=强）；孤儿由贴图+外壳半径表达
const strength={}, nodeEdgesNow={};
const histNow=e=>e.valid_to!=null; // now 态的时效（布局用；as-of 态另算）
const SECTOR={person:[0,200],org:[200,253],project:[253,306],artifact:[306,360]};
const P={};
function computeLayout(){
  for(const k in strength)delete strength[k];for(const k in nodeEdgesNow)delete nodeEdgesNow[k];for(const k in P)delete P[k];
  edges.forEach(e=>{for(const id of [e.a,e.b]){strength[id]=Math.max(strength[id]||0,e.observations||1);}});
  edges.forEach(e=>{(nodeEdgesNow[e.a]=nodeEdgesNow[e.a]||[]).push(e);(nodeEdgesNow[e.b]=nodeEdgesNow[e.b]||[]).push(e);});
  const buckets={};
  nodes.forEach(n=>{if(n.kind!=='self'&&n.kind!=='event'){const k=n.kind in SECTOR?n.kind:'person';(buckets[k]=buckets[k]||[]).push(n.id);}});
  for(const k of Object.keys(buckets))buckets[k].sort();
  const angleOf={};
  for(const [k,[a0,a1]] of Object.entries(SECTOR)){
    const ids=buckets[k]||[];
    ids.forEach((id,i)=>{angleOf[id]=(a0+((i+0.5)/ids.length)*(a1-a0))*Math.PI/180;});
  }
  // 时态下沉的分位数等化：cont 点按最近观察时间排名 → [0,1]
  const ageRank={};
  {
    const ages=[];
    nodes.forEach(n=>{
      if(n.kind==='self'||n.kind==='event')return;
      const es=nodeEdgesNow[n.id]||[];
      let last=0;
      es.forEach(e=>{const ts=e.last_observed_at||e.valid_from;if(ts){const v=new Date(ts).getTime();if(v>last)last=v;}});
      ages.push([n.id,last?(Date.now()-last):Infinity]);
    });
    ages.sort((a,b)=>a[1]-b[1]);
    ages.forEach(([id],i)=>{ageRank[id]=ages.length>1?i/(ages.length-1):0;});
  }
  let ei=0;const GA=Math.PI*(3-Math.sqrt(5));
  for(const n of nodes){
    const h=hash(n.id);
    if(n.kind==='self'){P[n.id]=new THREE.Vector3(0,0,0);continue;}
    if(n.kind==='event'){ // 发生者：底层环（终态即历史），全周
      const th=(ei++)*GA+h;const r=3.4-Math.min(strength[n.id]||1,6)*0.1;
      P[n.id]=new THREE.Vector3(Math.cos(th)*r,-1.8+h*0.5,Math.sin(th)*r);continue;}
    const th=angleOf[n.id]??h*Math.PI*2;
    const es=nodeEdgesNow[n.id]||[];
    const historical=es.length&&es.every(histNow);
    // y=连续时态下沉轴（§7-6 修订二：分位数等化）。真库审计发现年龄分布本身很紧
    // （多数实体最新证据在 5–15 天内），线性/对数任何度量映射都保团——按「距上次
    // 观察天数」的**排名**均匀铺满带（序保留、可区分由构造保证；精确天数进详情卡）。
    const y=0.35-(ageRank[n.id]??1)*1.1-(historical?0.5:0)+(h-0.5)*0.1;
    const r=es.length?(3.1-Math.min(strength[n.id]||1,10)*0.14):4.3; // 强=近；无边=外壳
    P[n.id]=new THREE.Vector3(Math.cos(th)*r,y,Math.sin(th)*r);
  }
}
computeLayout();

// ── 时间轴：真 bitemporal f(T)（valid_from/valid_to/created_at；可重算）──
let t0=new Date(), t1=new Date();
function computeTimeline(){
  const dates=[];
  edges.forEach(e=>{if(e.valid_from)dates.push(e.valid_from);});
  faces.forEach(f=>{if(f.created_at)dates.push(f.created_at);});
  dates.sort();
  t0=dates.length?new Date(dates[0]):new Date();
  t1=new Date();
}
computeTimeline();
const STEPS=24;
const slider=document.getElementById('time'), tlabel=document.getElementById('tlabel');
function tAt(step){return new Date(t0.getTime()+(t1.getTime()-t0.getTime())*step/STEPS);}
let T=t1;
const before=(a,b)=>!a||new Date(a)<=b;      // absent field ⇒ fail-open visible
const closed=vt=>vt!=null&&new Date(vt)<=T;
const edgeVisible=e=>before(e.valid_from,T);
const faceVisible=f=>before(f.created_at,T);
const edgeHist=e=>closed(e.valid_to);

// ── Three 场景（mockup 同款内敛风）────────────────────────────────
const view=document.getElementById('view');
const scene=new THREE.Scene(); scene.fog=new THREE.FogExp2(0x0b0c10,0.03);
const camera=new THREE.PerspectiveCamera(50,view.clientWidth/view.clientHeight,0.1,100);
const cameraScale=camera.aspect<0.65?1.65:(camera.aspect<1?1.25:1);
camera.position.set(0,8.5*cameraScale,10.5*cameraScale); // 窄屏后退；45° 俯视保留语义地板 + 面 surface
const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,preserveDrawingBuffer:true}); renderer.setPixelRatio(devicePixelRatio); renderer.setSize(view.clientWidth,view.clientHeight);
renderer.toneMapping=THREE.ACESFilmicToneMapping; view.appendChild(renderer.domElement);
const labelRenderer=new CSS2DRenderer(); labelRenderer.setSize(view.clientWidth,view.clientHeight); labelRenderer.domElement.style.cssText='position:absolute;top:0;pointer-events:none'; view.appendChild(labelRenderer.domElement);
const controls=new OrbitControls(camera,renderer.domElement); controls.enableDamping=true; controls.dampingFactor=.08; controls.target.set(0,1.4,0); controls.update();
const graph=new THREE.Group(); scene.add(graph);
const circleTex=(()=>{const c=document.createElement('canvas');c.width=c.height=128;const g=c.getContext('2d');g.beginPath();g.arc(64,64,60,0,Math.PI*2);g.fillStyle='#fff';g.fill();return new THREE.CanvasTexture(c);})();
const ringTex=(()=>{const c=document.createElement('canvas');c.width=c.height=128;const g=c.getContext('2d');g.beginPath();g.arc(64,64,54,0,Math.PI*2);g.lineWidth=10;g.setLineDash([16,12]);g.strokeStyle='#fff';g.stroke();return new THREE.CanvasTexture(c);})();
// 恒星光晕：柔和径向渐变（亮核 → 透明边），加法混合 → 星系辉光
const glowTex=(()=>{const c=document.createElement('canvas');c.width=c.height=128;const g=c.getContext('2d');
  const gr=g.createRadialGradient(64,64,0,64,64,64);
  gr.addColorStop(0,'rgba(255,255,255,1)');gr.addColorStop(0.18,'rgba(255,255,255,0.6)');
  gr.addColorStop(0.5,'rgba(255,255,255,0.16)');gr.addColorStop(1,'rgba(255,255,255,0)');
  g.fillStyle=gr;g.fillRect(0,0,128,128);return new THREE.CanvasTexture(c);})();
let nodeSprites=[],faceMeshes=[],bodyMeshes=[],labels3d=[];
// prio = 标签预算/碰撞剔除的优先级（大=先画、不被剔）；baseOp = 语义透明度（雾/hover 在其上乘）；
// full = hover title 全文（面的长句截断后靠它读全）。所有标签收进 labels3d 供每帧 cullLabels 处理。
function mkLabel(text,cls,colorHex,prio,baseOp,full,target){const d=document.createElement('div');d.className='lbl3d'+(cls?' '+cls:'');d.textContent=text;if(colorHex)d.style.color=colorHex;if(full)d.title=full;const o=new CSS2DObject(d);o.userData={label:true,prio:prio||0,baseOp:baseOp==null?1:baseOp,nid:null,fid:target?target.id:null};o.element.style.opacity=o.userData.baseOp;
  if(target){d.style.pointerEvents='auto';d.style.cursor='pointer'; // 面/体标签可交互：hover→setHover 高亮，click→pickTarget 选中
    d.addEventListener('mouseenter',()=>setHover({type:target.kind,id:target.id}));
    d.addEventListener('mouseleave',()=>setHover(null));
    d.addEventListener('click',ev=>{ev.stopPropagation();pickTarget(target.kind,target.id);});}
  labels3d.push(o);return o;}
function hullMesh(vs,color,opacity){let geo=null;
  if(vs.length>=4){try{geo=new ConvexGeometry(vs);}catch(e){geo=null;}}
  if(!geo||!geo.attributes.position||geo.attributes.position.count<3){
    geo=new THREE.BufferGeometry();const pts=[];
    for(let i=1;i<vs.length-1;i++)pts.push(vs[0].x,vs[0].y,vs[0].z,vs[i].x,vs[i].y,vs[i].z,vs[i+1].x,vs[i+1].y,vs[i+1].z);
    geo.setAttribute('position',new THREE.Float32BufferAttribute(pts,3));geo.computeVertexNormals();}
  return new THREE.Mesh(geo,new THREE.MeshBasicMaterial({color,transparent:true,opacity,side:THREE.DoubleSide,depthWrite:false}));}
function edgeMesh(A,B,color,radius,opacity){const dir=new THREE.Vector3().subVectors(B,A);const len=dir.length();
  const m=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,len,8,1,true),new THREE.MeshBasicMaterial({color,transparent:true,opacity,depthWrite:false}));
  m.position.copy(A).addScaledVector(dir,.5); m.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir.clone().normalize()); return m;}

// ── as-of 态 ───────────────────────────────────────────────────────
function computeState(){
  const vEdges=edges.filter(edgeVisible);
  const inComp=new Set(['self']);
  {const adj={};nodes.forEach(n=>adj[n.id]=[]);vEdges.forEach(e=>{if(adj[e.a])adj[e.a].push(e.b);if(adj[e.b])adj[e.b].push(e.a);});
   const q=['self'];while(q.length){const x=q.shift();(adj[x]||[]).forEach(y=>{if(!inComp.has(y)){inComp.add(y);q.push(y);}});}}
  const shadowIds=new Set(nodes.filter(n=>!inComp.has(n.id)).map(n=>n.id));
  const nodeEdges={};vEdges.forEach(e=>{(nodeEdges[e.a]=nodeEdges[e.a]||[]).push(e);(nodeEdges[e.b]=nodeEdges[e.b]||[]).push(e);});
  const validity={};nodes.forEach(n=>{const es=nodeEdges[n.id]||[];
    validity[n.id]=kcls(n)==='occ'?'terminal':(es.length&&es.every(edgeHist)?'historical':'live');});
  const mem={};nodes.forEach(n=>mem[n.id]=n.id==='self'?99:0);
  for(let i=0;i<6;i++)vEdges.forEach(e=>{const o=(e.observations||1)*(edgeHist(e)?.5:1);
    mem[e.a]=Math.max(mem[e.a]||0,Math.min(mem[e.b]||0,o));mem[e.b]=Math.max(mem[e.b]||0,Math.min(mem[e.a]||0,o));});
  return {vEdges,shadowIds,validity,mem};
}
function focusSets(vEdges){
  let HN=null,HE=null,HF=null;
  if(focus){const vis=faces.filter(faceVisible);
    if(focus.kind==='node'){
      HN=focus.treeIds?new Set(focus.treeIds):new Set([focus.id]);
      if(!focus.treeIds)vEdges.forEach(e=>{if(e.a===focus.id)HN.add(e.b);if(e.b===focus.id)HN.add(e.a);});
      HF=new Set(vis.filter(f=>(f.anchors||[]).includes(focus.id)).map(f=>f.id));
      HE=e=>HN.has(e.a)&&HN.has(e.b);}
    else{const f=vis.find(x=>x.id===focus.id);const mem=new Set(f?(f.anchors||[]):[]);mem.add('self');
      HN=mem;HF=new Set([focus.id]);HE=e=>mem.has(e.a)&&mem.has(e.b);}}
  return {HN,HE,HF};
}
function nodeColor(n,st){
  if(lens==='kind')return KIND[kcls(n)];
  if(lens==='validity'){const v=st.validity[n.id];return v==='terminal'?0x33507a:(v==='live'?0x3fb970:0x727a88);}
  if(lens==='mem'){const t=Math.min(1,(st.mem[n.id]||0)/5);return new THREE.Color(0x2a2f3a).lerp(new THREE.Color(0x62d6ff),t).getHex();}
  return 0x888888;
}
function edgeColor(e){if(edgeHist(e))return 0x5b636f; // 遮蔽序：historical > lens
  if(eLens==='mod')return FAM[PFAM[e.predicate]||'aff'];
  if(eLens==='val')return VAL[e.polarity||'0'];
  return 0x9aa3b2;}

// 面/体标签防重叠：多个面的质心挤在同一片锚点上时，贪心竖向错开
let placedLabels=[];
function placeLabel(pos){const p=pos.clone();
  for(let guard=0;guard<12;guard++){
    if(placedLabels.every(q=>q.distanceTo(p)>0.36))break;
    p.y+=0.19;}
  placedLabels.push(p.clone());return p;}
// ② 面的长签名 → 短关键词标签（引号里的词优先，否则剥「用户…」前缀取前 11 字）；全句进 hover title。
function faceKey(sig){sig=(sig||'').trim();
  const m=sig.match(/[「『“"']([^」』”"']{2,16})[」』”"']/);
  if(m)return m[1];
  return sig.replace(/^用户(倾向于|擅长|正在|会|对|以|建立了|是|发现|采用)?/,'').slice(0,11);}
function faceMark(level){return level===3?'根◎':(level===2?'体◆':'面▸');}
function faceFull(f,pfx){return pfx+(f.signature||'')+`  [${f.provenance}${f.status==='active'?'·转正✓':'·shadow'}]`;}
// 面/体的 n-case 分派（spec §7-6 完备表）：n≥3 凸包 · n=2 梭 · n=1 光环 · n=0 塔板
function renderFace(f,anchorPts,col,both,o,tagPrefix){
  const hex='#'+col.getHexString();
  const tag=tagPrefix+faceKey(f.signature)+(f.status==='active'?' ✓':'');
  const full=faceFull(f,tagPrefix), fprio=40+Math.min(f.observations||1,10);
  const isBody=f.level>=2;
  const frameOp=(both||isBody?0.85:0.45)*o, fillOp=(isBody?0.03:(both?0.15:0.05))*o;
  // ★ 真实语义位置优先：面若带 fact_pts（成员事实的真实 embedding 相对位置），就把每个事实画在
  // 它的真实位置上、凸包连成簇 = 面从事实的真实语义分布中涌现（不再凭空造花瓣/凸包锚点）。
  if(f.fact_pts&&f.fact_pts.length>=3){
    const c=anchorPts.length?anchorPts.reduce((s,v)=>s.add(v.clone()),new THREE.Vector3()).multiplyScalar(1/anchorPts.length):(P['self']?P['self'].clone():new THREE.Vector3());
    const R=0.24+0.12*Math.sqrt(f.fact_pts.length);
    const fpts=f.fact_pts.map((p,i)=>new THREE.Vector3(c.x+p[0]*R*0.7, c.y+p[1]*R*0.7, c.z+(i%2?0.012:-0.012)));
    const fill=hullMesh(fpts,col,(both||isBody?0.15:0.08)*o);fill.userData={kind:'face',id:f.id};graph.add(fill);(isBody?bodyMeshes:faceMeshes).push(fill);
    const eg=new THREE.LineSegments(new THREE.EdgesGeometry(fill.geometry),(both||isBody)?new THREE.LineBasicMaterial({color:col,transparent:true,opacity:frameOp}):new THREE.LineDashedMaterial({color:col,transparent:true,opacity:frameOp,dashSize:.1,gapSize:.08}));
    if(!(both||isBody))eg.computeLineDistances();graph.add(eg);
    for(const p of fpts){const d=new THREE.Sprite(new THREE.SpriteMaterial({map:circleTex,color:col,transparent:true,opacity:0.6*o,depthTest:false,depthWrite:false}));d.scale.setScalar(0.05);d.position.copy(p);d.renderOrder=1;graph.add(d);}
    const cc=fpts.reduce((s,v)=>s.add(v.clone()),new THREE.Vector3()).multiplyScalar(1/fpts.length);
    const lab=mkLabel(tag+'·'+f.fact_pts.length+'个事实',both?'sm':'sm ghost',hex,fprio,o,full,{kind:'face',id:f.id});lab.position.copy(placeLabel(cc.add(new THREE.Vector3(0,R+0.1,0))));graph.add(lab);
    return true;
  }
  if(anchorPts.length>=3){
    const fill=hullMesh(anchorPts,col,fillOp);
    fill.userData={kind:'face',id:f.id};graph.add(fill);(isBody?bodyMeshes:faceMeshes).push(fill);
    const mat=(both||isBody)?new THREE.LineBasicMaterial({color:col,transparent:true,opacity:frameOp})
      :new THREE.LineDashedMaterial({color:col,transparent:true,opacity:frameOp,dashSize:.12,gapSize:.1});
    const eg=new THREE.LineSegments(new THREE.EdgesGeometry(fill.geometry),mat);
    if(!(both||isBody))eg.computeLineDistances();
    graph.add(eg);
    const c=anchorPts.reduce((s,v)=>s.add(v.clone()),new THREE.Vector3()).multiplyScalar(1/anchorPts.length);
    const lab=mkLabel(tag,both?'sm':'sm ghost',hex,fprio,o,full,{kind:'face',id:f.id});lab.position.copy(placeLabel(c.add(new THREE.Vector3(0,0.12,0))));graph.add(lab);
    return true;
  }
  if(anchorPts.length>=1&&anchorPts.length<3){ // 1-2 实体锚点撑不起 2 维 → 把面的 N 个「事实点」撒出来、连成 2D 区域
    // 面=fact集（点=fact，维度判据）。实体 anchors 只是 1-2 点的薄投影 —— 面真正的点是它的 fact
    // 成员。所以在锚点旁按 phyllotaxis（向日葵）撒 n_members 个事实点，hullMesh 连成凸包 = 真正的
    // 「N 个事实连成的面」，不再是抽象圆圈。事实点用小圆点画出，一眼看清面由多少事实撑起。
    const c=anchorPts.reduce((s,v)=>s.add(v.clone()),new THREE.Vector3()).multiplyScalar(1/anchorPts.length);
    const nm=Math.max(3,f.n_members||3), R=0.22+0.115*Math.sqrt(nm); // 簇半径 ∝ √(fact数)
    const GA=2.399963, fpts=[]; // 黄金角 → 均匀不重叠的向日葵散布
    for(let i=0;i<nm;i++){const rr=R*Math.sqrt((i+0.5)/nm), a=i*GA;
      fpts.push(new THREE.Vector3(c.x+rr*Math.cos(a), c.y+rr*Math.sin(a), c.z+(i%2?0.012:-0.012)));}
    const fill=hullMesh(fpts,col,(both||isBody?0.16:0.09)*o); // 面 = 事实点的凸包（2D 区域）
    fill.userData={kind:'face',id:f.id};graph.add(fill);(isBody?bodyMeshes:faceMeshes).push(fill);
    const eg=new THREE.LineSegments(new THREE.EdgesGeometry(fill.geometry),
      (both||isBody)?new THREE.LineBasicMaterial({color:col,transparent:true,opacity:frameOp})
      :new THREE.LineDashedMaterial({color:col,transparent:true,opacity:frameOp,dashSize:.1,gapSize:.08}));
    if(!(both||isBody))eg.computeLineDistances();graph.add(eg);
    for(const p of fpts){const d=new THREE.Sprite(new THREE.SpriteMaterial({map:circleTex,color:col,transparent:true,opacity:0.55*o,depthTest:false,depthWrite:false})); // 每个事实一个小圆点
      d.scale.setScalar(0.05);d.position.copy(p);d.renderOrder=1;graph.add(d);}
    const lab=mkLabel(tag+'·'+nm+'个事实',both?'sm':'sm ghost',hex,fprio,o,full,{kind:'face',id:f.id});lab.position.copy(placeLabel(c.clone().add(new THREE.Vector3(0,R+0.12,0))));graph.add(lab);
    return true;
  }
  return false; // n=0：塔板 fallback（调用方收集）
}

function renderModelTower(items,z=-2.7){
  const byLevel={1:[],2:[],3:[]};
  items.forEach(f=>(byLevel[f.level]||byLevel[1]).push(f));
  for(const lvl of [1,2,3]){
    const row=byLevel[lvl], y=1.9+(lvl-1)*0.85;
    row.forEach((f,i)=>{
      const x=(i-(row.length-1)/2)*1.15;
      const both=f.provenance==='both', col=idColor(f.id), root=lvl===3;
      const radius=root?0.5:0.34+Math.min(f.observations||1,6)*0.03;
      const plate=new THREE.Mesh(new THREE.CircleGeometry(radius,32),
        new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:both||root?0.52:0.18,side:THREE.DoubleSide,depthWrite:false}));
      plate.rotation.x=-Math.PI/2;plate.position.set(x,y,z);plate.userData={kind:'face',id:f.id};graph.add(plate);(lvl>=2?bodyMeshes:faceMeshes).push(plate);
      const ring=new THREE.Mesh(new THREE.RingGeometry(radius+0.02,radius+0.06,32),
        new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:both||root?0.92:0.4,side:THREE.DoubleSide,depthWrite:false}));
      ring.rotation.x=-Math.PI/2;ring.position.set(x,y,z);graph.add(ring);
      const pfx=faceMark(lvl);
      const lab=mkLabel(pfx+faceKey(f.signature)+(f.status==='active'?' ✓':''),both||root?'sm':'sm ghost','#'+col.getHexString(),50+lvl*10,1,faceFull(f,pfx),{kind:'face',id:f.id});
      lab.position.set(x,y+0.15,z);graph.add(lab);
    });
  }
}

function build(){
  while(graph.children.length){const o=graph.children.pop();o.traverse(x=>{if(x.isCSS2DObject&&x.element)x.element.remove();});}
  nodeSprites=[];faceMeshes=[];bodyMeshes=[];placedLabels=[];labels3d=[];
  // 统一语义 fact-空间为主视图：同一批 fact 换到语义坐标域（XZ=embedding，Y=时间），
  // fact 间连接 + 连接上涌现的面/体。有 sem_geo 时只渲这一个，跳过旧力导向实体图。
  if((semGeo.facts||[]).length){
    renderSemSpace();
    const modelFaces=faces.filter(faceVisible);
    renderModelTower(modelFaces);
    const nf=semGeo.facts.length, ne=(semGeo.edges||[]).length;
    const nface=modelFaces.filter(f=>f.level===1).length, nvolume=modelFaces.filter(f=>f.level===2).length, nroot=modelFaces.filter(f=>f.level===3).length;
    document.getElementById('legend').innerHTML=
      '<div class="sec">点 = 事实（最小单元）· 在语义地板</div>'+
      '<div class="row"><span class="dot" style="background:#7aa0ff"></span>色相 = 所属面（空间簇），亮/大 = 连接度高</div>'+
      '<div class="row"><span class="dot" style="background:#555a66"></span>灰 = 不属于任何面（孤立事实）</div>'+
      '<div class="sec">坐标轴</div>'+
      '<div class="row"><span class="bar" style="border-color:#8fe0ea"></span>XZ 平面 = 语义布局（同簇聚拢·簇间按关联展开）</div>'+
      '<div class="row"><span class="bar" style="border-color:#c9a0ff"></span>Y 轴 = 涌现层级（点→面→体，越高越抽象）</div>'+
      '<div class="row" style="opacity:.75">时间 → 底部 as-of 拖动条（拖它=按沉积时间淡入）</div>'+
      '<div class="sec">连接 = k-NN 语义相似</div>'+
      '<div class="row"><span class="bar" style="border-color:#5a8fb0"></span>粗/亮 = 更相似</div>'+
      '<div class="sec">涌现（上下链接的纵向层级）</div>'+
      '<div class="row"><span class="dot" style="background:transparent;border:2px solid #b58bd6"></span>面▸ = 一个空间簇（k-NN 社区）的凸包轮廓</div>'+
      '<div class="row"><span class="bar" style="border-color:#b58bd6"></span>主干 = 事实簇 → 面（点→面 上下链接）</div>'+
      '<div class="row" style="opacity:.8">越大的面升得越高 → 纵向层级梯度</div>'+
      '<div class="row"><span class="dot" style="background:#d6b58b"></span>体◆ = 面的面（更高一层涌现，成体才出现）</div>'+
      '<div class="row"><span class="dot" style="background:#c65cff"></span>根◎ = 单一模型顶点（可展开回收据）</div>'+
      '<div class="row" style="opacity:.7">悬停任一点 → 右侧看事实内容 + 邻居 + 所属面</div>';
    document.getElementById('stats').innerHTML=
      `事实(点) <b>${nf}</b> · 语义连接(边) <b>${ne}</b> · 面 <b>${nface}</b> · 体 <b>${nvolume}</b> · 根 <b>${nroot}</b><br>`+
      `<span style="font-size:11px">XZ=语义空间 · Y=涌现层级(点→面→体→根) · 时间=底部拖动条 · 点亮度∝连接度 · 悬停看内容</span>`;
    return;
  }
  const st=computeState();
  const {vEdges,shadowIds,validity}=st;
  const {HN,HE,HF}=focusSets(vEdges);
  const nOp=id=>HN?(HN.has(id)?1:.12):1, eOp=e=>HE?(HE(e)?1:.06):.85, fOp=id=>HF?(HF.has(id)?1:.12):1;
  const touched=new Set(['self']);vEdges.forEach(e=>{touched.add(e.a);touched.add(e.b);});
  let nP=0,nA=0,nEact=0,nEsh=0,nHist=0;
  // ── 边 ──
  for(const e of vEdges){
    const A=P[e.a],B=P[e.b];if(!A||!B)continue;
    const hist=edgeHist(e);
    const active=e.status==='active';
    const col=edgeColor(e);
    const radius=hist?0.006:(0.006+Math.min(e.observations||1,6)*0.007);
    const o=eOp(e)*(hist?0.3:(active?1:0.2)); // shadow 恒暗（§3.3 状态闸）
    graph.add(edgeMesh(A,B,col,radius,Math.min(.92,o)));
    if(active&&!hist)nEact++;else if(!hist)nEsh++;else nHist++;
    if(eLens==='dir'&&!hist){const dir=new THREE.Vector3().subVectors(B,A).normalize();
      const tip=B.clone().addScaledVector(dir,-.16);
      const ar=new THREE.Mesh(new THREE.ConeGeometry(.045,.13,10),new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:o}));
      ar.position.copy(tip);ar.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir);graph.add(ar);
      if(SYM.has(e.predicate)){const t2=A.clone().addScaledVector(dir,.16);
        const a2=new THREE.Mesh(new THREE.ConeGeometry(.045,.13,10),new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:o}));
        a2.position.copy(t2);a2.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir.clone().negate());graph.add(a2);}}
    if(!hist&&o>.35&&(active||(e.observations||1)>=3)){ // 标签控噪：active 或强证据
      const lc=A.clone().add(B).multiplyScalar(.5);
      const txt=(e.label||e.predicate)+((e.observations||1)>1?` ×${e.observations}`:'');
      const lab=mkLabel(txt,'sm','#'+col.toString(16).padStart(6,'0'),8+Math.min(e.observations||1,10),o);lab.position.copy(lc);graph.add(lab);}
  }
  // ── 点 ──
  for(const n of nodes){
    const cls=kcls(n);
    const isShadow=shadowIds.has(n.id);
    if(cls!=='user'&&!touched.has(n.id)&&!isShadow)continue;
    const strT=Math.min(strength[n.id]||1,12)/12;
    let col=isShadow?0x8a93a3:nodeColor(n,st);
    if(!isShadow&&cls==='cont'){ // 亮度 ∝ 证据强度：强=明度更高（保色相），弱=暗
      const c=new THREE.Color(col),hsl={};c.getHSL(hsl);c.setHSL(hsl.h,hsl.s,Math.min(0.92,hsl.l*(0.62+0.85*strT)));col=c.getHex();}
    const o=nOp(n.id)*(validity[n.id]==='historical'?.5:1)*(isShadow?.6:1);
    const coreSz=cls==='user'?0.32:(cls==='occ'?0.075:0.095), spOp=cls==='occ'?Math.min(o,.9):o; // 核=固定小亮点
    let glow=null;
    if(!isShadow){ // 恒星光晕：size ∝ 强度（亮度越大光晕越大），加法混合 → 星系辉光
      const haloSz=cls==='user'?1.25:(cls==='occ'?0.24:0.28+strT*0.7);
      const gOp=(cls==='occ'?0.3:(cls==='user'?0.75:0.38+strT*0.4))*o;
      glow=new THREE.Sprite(new THREE.SpriteMaterial({map:glowTex,color:col,transparent:true,opacity:gOp,depthTest:false,depthWrite:false,blending:THREE.AdditiveBlending}));
      glow.scale.setScalar(haloSz);glow.position.copy(P[n.id]);glow.renderOrder=2;glow.userData={glow:true,baseScale:haloSz,baseOp:gOp};graph.add(glow);}
    const core=new THREE.Sprite(new THREE.SpriteMaterial({map:isShadow?ringTex:circleTex,color:col,transparent:true,opacity:spOp,depthTest:false,depthWrite:false}));
    core.scale.setScalar(coreSz);core.position.copy(P[n.id]);core.renderOrder=3;core.userData={kind:'node',id:n.id,target:P[n.id].clone(),baseScale:coreSz,baseOp:spOp,glow};graph.add(core);nodeSprites.push(core);
    if(cls==='cont'){nP++;}else if(cls==='occ'){nA++;}
    if(cls!=='occ'||isShadow){
      const prio=cls==='user'?1000:(isShadow?20:60+Math.min(strength[n.id]||1,30));
      const lab=mkLabel(n.label+(isShadow?'（孤儿·shadow）':''),cls==='user'?null:(isShadow?'ghost':'sm'),prio,o);
      lab.userData.nid=n.id;
      lab.position.copy(P[n.id]).add(new THREE.Vector3(0,coreSz+0.14,0));graph.add(lab);}
  }
  // ── 面/体（角=锚点；面不含 USER，体锚 USER 一角）──
  const plates=[];
  const visFaces=faces.filter(faceVisible);
  visFaces.forEach(f=>{
    const isBody=f.level>=2;
    if((f.level===1&&!showSchema)||(f.level===2&&!showBody))return;
    const both=f.provenance==='both';
    const col=idColor(f.id);
    const o=fOp(f.id);
    // 角=可见非孤儿锚点（孤儿在外壳，不参与涌现簇的形状）
    const anchorPts=(f.anchors||[]).filter(a=>P[a]&&touched.has(a)&&!shadowIds.has(a)).map(a=>P[a].clone());
    if(isBody&&anchorPts.length>=1)anchorPts.push(P['self'].clone()); // §1.5-3 体锚根
    if(!renderFace(f,anchorPts,col,both,o,faceMark(f.level)))plates.push(f);
  });
  const byLevel={1:[],2:[],3:[]};
  plates.forEach(f=>{(byLevel[f.level]||byLevel[1]).push(f);});
  for(const lvl of [1,2,3]){
    const row=byLevel[lvl];const y=1.9+(lvl-1)*0.85;
    row.forEach((f,i)=>{
      const x=(i-(row.length-1)/2)*1.15;
      const both=f.provenance==='both';
      const col=idColor(f.id);
      const o=fOp(f.id);
      const plate=new THREE.Mesh(new THREE.CircleGeometry(0.34+Math.min(f.observations||1,6)*0.03,32),
        new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:(both?0.5:0.16)*o,side:THREE.DoubleSide,depthWrite:false}));
      plate.rotation.x=-Math.PI/2;plate.position.set(x,y,0);plate.userData={kind:'face',id:f.id};graph.add(plate);faceMeshes.push(plate);
      const ring=new THREE.Mesh(new THREE.RingGeometry(0.36,0.4,32),
        new THREE.MeshBasicMaterial({color:col,transparent:true,opacity:(both?0.9:0.35)*o,side:THREE.DoubleSide,depthWrite:false}));
      ring.rotation.x=-Math.PI/2;ring.position.set(x,y,0);graph.add(ring);
      const pfx=faceMark(lvl);
      const lab=mkLabel(pfx+faceKey(f.signature)+(f.status==='active'?' ✓':''),both?'sm':'sm ghost','#'+col.getHexString(),40+Math.min(f.observations||1,10),o,faceFull(f,pfx),{kind:'face',id:f.id});
      lab.position.set(x,y+0.14,0);graph.add(lab);
    });
  }
  drawLegend(st.shadowIds.size);
  document.getElementById('stats').innerHTML=
    `人/实体 <b>${nP}</b> · Activity <b>${nA}</b> · 边 <b>${nEact} active / ${nEsh} shadow / ${nHist} 已结束</b> · 面/体 <b>${visFaces.length}</b>（${plates.length} 无锚塔板）<br>`+
    `<span style="font-size:11px">as-of ${T.toISOString().slice(0,10)} · 扇区=种类 · 高度=时态 · 近=证据强</span><br>`+
    `<span style="font-size:11px">检索权重: 文本1.0 · 槽${searchState.slot_pool_weight??'?'} · 关系${searchState.relation_pool_weight??'?'} · shadow喂食${searchState.relation_include_shadow?'开(×0.5)':'关'} · 池内混排${searchState.contains_pool_rerank?'开(recency⊕sim)':'关'}</span>`;
}
function drawLegend(nShadow){const L=document.getElementById('legend');const hx=c=>'#'+c.toString(16).padStart(6,'0');
  const nS={kind:[[KIND.user,'USER'],[KIND.cont,'持续者'],[KIND.occ,'发生者(终态事件)']],
            validity:[[0x3fb970,'live'],[0x727a88,'historical(边全收口)'],[0x33507a,'发生者(终态即历史)']],
            mem:[[0x62d6ff,'记忆度高(近USER·证据强)'],[0x2a2f3a,'低(易被遗忘)']]}[lens]||[];
  let h='<div class="sec">点（扇区=种类 · 高=新近/沉=久未观察 · 近=强）</div>'+nS.map(([c,t])=>`<div class="row"><span class="dot" style="background:${hx(c)}"></span>${t}</div>`).join('');
  h+='<div class="row"><span class="dot" style="background:transparent;border:2px dashed #8a93a3"></span>孤儿=shadow(TTL内)</div>';
  const eS={mod:[[FAM.struct,'结构(part_of/reports_to/depends_on)'],[FAM.dyn,'动态(participates_in)'],[FAM.sem,'指涉(about)'],[FAM.aff,'亲和(knows)']],
            dir:[[0x9aa3b2,'→ 规范方向 / ↔ knows 双头']],
            val:[[VAL['+'],'+'],[VAL['-'],'−'],[VAL['0'],'0(中性)']]}[eLens]||[];
  h+='<div class="sec">边（粗细=observations 证据数）</div>'+eS.map(([c,t])=>`<div class="row"><span class="bar" style="border-color:${hx(c)}"></span>${t}</div>`).join('');
  h+='<div class="row"><span class="bar" style="border-color:#5b636f;border-top-style:dashed"></span>historical(仍连通)</div>';
  h+='<div class="row"><span class="bar" style="border-color:#4a5160"></span>shadow status(恒暗·检索盲)</div>';
  h+='<div class="sec">面/体（颜色=身份色 · 角=锚点）</div>';
  h+='<div class="row"><span class="dot" style="background:#7a9;opacity:.6;border-radius:3px"></span>both=实线亮框(转正)</div>';
  h+='<div class="row"><span class="dot" style="background:transparent;border:2px dashed #7a9;border-radius:3px"></span>单路=虚线弱框(shadow)</div>';
  h+='<div class="row" style="font-size:11px">n≥3 凸包 · n=2 梭 · n=1 光环 · n=0 塔板；体多锚 USER 一角</div>';
  h+=`<div class="sec">收敛</div><div class="row" style="color:var(--live)">✓ 连通=①engaged地板 ⊕ ②语义边</div><div class="row" style="color:var(--dim)">孤儿:${nShadow} 个（无边或弱地板·TTL 即遗忘）</div>`;
  L.innerHTML=h;}
// ── 详情 + 拾取 ────────────────────────────────────────────────────
const detail=document.getElementById('detail');
// 详情面板里点「上级规律」链接 → 选中那个 Schema/体（点/面/体统一跳转）
detail.addEventListener('click',ev=>{const el=ev.target.closest('.lnk');if(el&&el.dataset.fid)pickTarget('face',el.dataset.fid);});
function showDetail(kind,id){
  if(kind==='fact'){const fi=+id;const f=semGeo.facts[fi];if(!f)return;
    // 邻居（k-NN 语义相似连接）+ 所属面/体（涌现）
    const nb=[];for(const [a,b,w] of (semGeo.edges||[])){if(a===fi)nb.push([b,w]);else if(b===fi)nb.push([a,w]);}
    nb.sort((x,y)=>y[1]-x[1]);
    const deg=nb.length;
    const mem=(semGeo.faces||[]).filter(fc=>(fc.members||[]).includes(fi));
    const nbHtml=nb.slice(0,8).map(([j,w])=>`<div style="margin:2px 0;font-size:12px;opacity:${0.5+w*0.5}"><span style="color:var(--dim)">~${w.toFixed(2)}</span> ${(semGeo.facts[j]||{}).t||''}</div>`).join('')||'<span style="color:var(--dim)">无</span>';
    const memHtml=mem.length?mem.map(fc=>`<div style="margin:2px 0;color:${fc.level>=2?'var(--live)':'var(--ink)'};font-size:12px">${faceMark(fc.level)} ${fc.sig||''}（涌现自 ${(fc.members||[]).length} 个事实）</div>`).join(''):'<span style="color:var(--dim)">尚未涌现出面</span>';
    const point=(modelSnapshot.points||[]).find(p=>(p.content||'')===(f.t||''));
    const receiptHtml=point&&point.receipt?`<div style="margin:7px 0;color:var(--dim)"><b>Receipt</b> <code>${point.receipt}</code></div>`:'';
    detail.innerHTML=`<h3>事实（点）</h3><div style="font-size:13px;line-height:1.5;margin:4px 0 8px">${f.t||''}</div>`+
      `<b>语义连接</b> ${deg} 条 · 时间 ${(f.t2||'').slice(0,10)||'—'}<br>${receiptHtml}`+
      `<div style="color:var(--ink);font-weight:600;font-size:11px;margin:8px 0 2px">最相近的事实（k-NN 邻居）</div>${nbHtml}`+
      `<div style="color:var(--ink);font-weight:600;font-size:11px;margin:8px 0 2px">上级 · 归纳出的规律（Schema▸ / 体◆）</div>${memHtml}`;
    detail.style.display='block';return;}
  if(kind==='node'){const n=byId[id];const cls=kcls(n);
    const st=cls==='occ'?'发生者(Activity·终态才入图)':cls==='user'?'根':`持续者(${n.kind})`;
    const es=edges.filter(e=>e.a===id||e.b===id);
    const preds={};es.forEach(e=>preds[e.predicate]=(preds[e.predicate]||0)+1);
    const predLine=Object.entries(preds).map(([k,v])=>`${k}×${v}`).join(' · ')||'无边';
    // 上级：这个点归纳成了哪些 Schema（面）/ 体（面的面）——面/体 anchors 含此点即其上级规律
    const mine=faces.filter(f=>(f.anchors||[]).includes(id)).sort((a,b)=>b.level-a.level||(b.observations||0)-(a.observations||0));
    const lin=mine.length?`<div style="color:var(--ink);font-weight:600;font-size:11px;margin:8px 0 2px">上级 · 归纳出的规律（Schema▸ / 体◆）</div>`+
      mine.map(f=>`<div class="lnk" data-fid="${f.id}" title="${(f.signature||'').replace(/"/g,'&quot;')}" style="cursor:pointer;margin:2px 0;padding-left:2px;color:${f.status==='active'?'var(--live)':'var(--dim)'}">${faceMark(f.level)} ${faceKey(f.signature)}${f.status==='active'?' ✓转正':' ·shadow'}</div>`).join(''):'';
    detail.innerHTML=`<h3>${n.label}</h3><b>${st}</b> · kind=${n.kind} · 边 ${predLine}${lin}<br><span class="raw" style="color:var(--dim)">原始记忆加载中…</span>`;
    // §2.1 每个点指回符号收据：懒取该点蒸馏自的原始条目
    fetch('/model/node?id='+encodeURIComponent(id)).then(r=>r.json()).then(d=>{
      if(!focus||focus.id!==id)return;
      // 以该点为根的关系树（§3.4 路径即叙事，根=这个事物）
      function treeHtml(node,depth){
        if(!node||!node.edges||!node.edges.length)return '';
        return node.edges.map(e=>{
          const arrow=e.dir==='out'?'→':'←';
          const dim=e.status==='active'?'':'opacity:.55;';
          const hist=e.historical?'（已结束）':'';
          const lbl=byId[e.child.id]?byId[e.child.id].label:e.child.id;
          return `<div style="margin-left:${depth*13}px;${dim}font-size:12px">`+
            `${arrow} <span style="color:var(--dim)">${e.label||e.predicate}${e.observations>1?' ×'+e.observations:''}${hist}</span> `+
            `<b>${lbl}</b></div>`+treeHtml(e.child,depth+1);
        }).join('');
      }
      const tree=treeHtml(d.tree,0);
      const lines=(d.raw||[]).map(r=>`<div style="margin:4px 0;border-left:2px solid var(--line);padding-left:7px">`+
        `<span style="font-size:10.5px;color:var(--dim)">${(r.ts||'').slice(0,16)}</span><br>${r.text}</div>`).join('');
      const el=detail.querySelector('.raw');
      if(el)el.outerHTML=
        (tree?`<div class="raw"><div style="color:var(--ink);font-weight:600;font-size:11px;margin:5px 0 2px">关系树（以此为根）</div>${tree}`:`<div class="raw">`)+
        (lines?`<div style="color:var(--ink);font-weight:600;font-size:11px;margin:7px 0 2px">原始记忆</div>${lines}`:`<div style="color:var(--dim)">（${d.source||'无来源'}：暂无原始条目）</div>`)+
        `</div>`;
      // 树上的点在图中聚焦高亮
      const ids=new Set([id]);
      (function walk(n){(n.edges||[]).forEach(e=>{ids.add(e.child.id);walk(e.child);});})(d.tree||{});
      if(focus&&focus.id===id){focus={kind:'node',id,treeIds:ids};build();}
    }).catch(()=>{});}
  else{const f=faces.find(x=>x.id===id);if(!f)return;
    const both=f.provenance==='both';
    detail.innerHTML=`<h3>${faceMark(f.level)}${f.signature||''}</h3>统一 schema（level${f.level}）· 由 <b>${f.n_members||0}</b> 个事实聚成（面的点=事实，≥3 才成面）· provenance=<b>${f.provenance}</b>${both?'（双信号转正 ✓）':'（单路 shadow）'} · obs=${f.observations}<br>关于（锚=主体，非点）：${(f.anchors||[]).join('、')||'（无锚·塔板）'}`;}
  detail.style.display='block';}
const ray=new THREE.Raycaster(),mouse=new THREE.Vector2();let downXY=null;
renderer.domElement.addEventListener('pointerdown',e=>downXY={x:e.clientX,y:e.clientY});
renderer.domElement.addEventListener('pointerup',e=>{if(!downXY)return;const moved=Math.abs(e.clientX-downXY.x)+Math.abs(e.clientY-downXY.y)>4;downXY=null;if(moved)return;
  const r=renderer.domElement.getBoundingClientRect();mouse.x=((e.clientX-r.left)/r.width)*2-1;mouse.y=-((e.clientY-r.top)/r.height)*2+1;ray.setFromCamera(mouse,camera);
  let hit=null;for(const grp of [nodeSprites,faceMeshes,bodyMeshes]){const ins=ray.intersectObjects(grp,false);if(ins.length){hit=ins[0].object.userData;break;}}
  if(hit){focus=(focus&&focus.kind===hit.kind&&focus.id===hit.id)?null:{kind:hit.kind,id:hit.id};if(focus)showDetail(hit.kind,hit.id);else detail.style.display='none';}
  else{focus=null;detail.style.display='none';}
  build();});
// ── UI ─────────────────────────────────────────────────────────────
document.querySelectorAll('.toolbar [data-lens]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.toolbar [data-lens]').forEach(x=>x.classList.remove('on'));b.classList.add('on');lens=b.dataset.lens;build();}));
document.querySelectorAll('.toolbar [data-elens]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.toolbar [data-elens]').forEach(x=>x.classList.remove('on'));b.classList.add('on');eLens=b.dataset.elens;build();}));
document.getElementById('schemaBtn').addEventListener('click',function(){showSchema=!showSchema;this.classList.toggle('on',showSchema);build();});
document.getElementById('bodyBtn').addEventListener('click',function(){showBody=!showBody;this.classList.toggle('on',showBody);build();});
document.getElementById('spinBtn').addEventListener('click',function(){controls.autoRotate=!controls.autoRotate;controls.autoRotateSpeed=1.2;this.classList.toggle('on',controls.autoRotate);});
function setT(step){const s=Math.max(0,Math.min(STEPS,step));slider.value=s;T=s>=STEPS?t1:tAt(s);tlabel.textContent=s>=STEPS?'now':T.toISOString().slice(0,10);build();}
slider.addEventListener('input',()=>setT(+slider.value));
let play=null;document.getElementById('play').addEventListener('click',function(){
  if(play){clearInterval(play);play=null;this.textContent='▶';return;}
  this.textContent='⏸';if(+slider.value>=STEPS)setT(0);
  play=setInterval(()=>{const s=+slider.value;if(s>=STEPS){clearInterval(play);play=null;document.getElementById('play').textContent='▶';return;}setT(s+1);},700);});
build();  // 首次构建（explode() 挪到脚本末尾触发，避开 spawnStart 的 TDZ）

// ── 实时轮询：重取 → 重算布局/时间轴 → 重建（位置确定性=已有点留原位、新点平滑出现，
//    延时摄影用）。停在 now 时跟随新边；相机/镜头/焦点全程保留。────────────────
async function refresh(){
  try{
    const atNow=(+slider.value>=STEPS);
    ingest(await (await fetch('/model/graph')).json());
    computeLayout(); computeTimeline();
    if(atNow){T=t1;tlabel.textContent='now';}
    build();
  }catch(e){/* 网络抖动/写锁竞争 → 本轮跳过，下轮再来 */}
}
setInterval(refresh, 4000);

// ── ⑤ hover 聚焦 + 标签选中：悬停/点击 点 或 面/体标签 → 高亮/选中，其余调暗（每帧，不重建）──
let hover=null, hoverSet=new Set(), hoverFaceId=null, hoverFactSet=null;
function setHover(h){
  const key=h?h.type+':'+h.id:null, cur=hover?hover.type+':'+hover.id:null;
  if(key===cur)return;
  hover=h; hoverSet=new Set(); hoverFaceId=null; hoverFactSet=null;
  if(h&&h.type==='fact'){hoverFactSet=new Set([h.id]);
    for(const [a,b] of (semGeo.edges||[])){if(a===h.id)hoverFactSet.add(b);else if(b===h.id)hoverFactSet.add(a);}}
  else if(h&&h.type==='node'){hoverSet.add(h.id);edges.forEach(e=>{if(e.a===h.id)hoverSet.add(e.b);if(e.b===h.id)hoverSet.add(e.a);});}
  else if(h&&h.type==='face'){const f=faces.find(x=>x.id===h.id);if(f){(f.anchors||[]).forEach(a=>hoverSet.add(a));hoverSet.add('self');}hoverFaceId=h.id;}
  renderer.domElement.style.cursor=h?'pointer':'default';
}
function pickTarget(kind,id){ // 点击选中（点/面通用，与画布拾取同一套 focus 逻辑）
  focus=(focus&&focus.kind===kind&&focus.id===id)?null:{kind,id};
  if(focus)showDetail(kind,id);else detail.style.display='none';
  build();
}
renderer.domElement.addEventListener('pointermove',ev=>{
  const r=renderer.domElement.getBoundingClientRect();
  mouse.x=((ev.clientX-r.left)/r.width)*2-1;mouse.y=-((ev.clientY-r.top)/r.height)*2+1;ray.setFromCamera(mouse,camera);
  const ins=ray.intersectObjects(nodeSprites,false);
  const ud=ins.length?ins[0].object.userData:null;
  if(ud&&ud.kind==='fact'){setHover({type:'fact',id:ud.fi});showDetail('fact',ud.fi);}
  else{setHover(ud?{type:'node',id:ud.id}:null);
    if(!ud&&!focus)detail.style.display='none';}
});
renderer.domElement.addEventListener('pointerleave',()=>setHover(null));
function applyHover(){
  const on=!!hover;
  for(const sp of nodeSprites){
    if(sp.userData.kind==='fact'){const base=sp.userData.baseOp==null?0.9:sp.userData.baseOp;
      sp.material.opacity=base*(hoverFactSet?(hoverFactSet.has(sp.userData.fi)?1:0.12):1);continue;}
    const base=sp.userData.baseOp==null?1:sp.userData.baseOp;
    const f=on?(hoverSet.has(sp.userData.id)?1:0.14):1;
    sp.material.opacity=base*f;
    const g=sp.userData.glow;if(g)g.material.opacity=g.userData.baseOp*f;}
}
// ── ①④ 标签预算 + 碰撞剔除 + 景深雾化（每帧）：按优先级贪心占位，重叠/超预算即隐藏，远则淡 ──
const tmpV=new THREE.Vector3();
function cullLabels(){
  const W=view.clientWidth,H=view.clientHeight, items=[];
  for(const o of labels3d){const el=o.element;if(!el)continue;
    o.getWorldPosition(tmpV);const dist=camera.position.distanceTo(tmpV);
    tmpV.project(camera);
    if(tmpV.z>1||tmpV.z<-1){el.classList.add('hide');continue;}
    const sx=(tmpV.x*0.5+0.5)*W, sy=(-tmpV.y*0.5+0.5)*H;
    const fade=Math.max(0.12,Math.min(1,1.75-(dist-5.5)/10)); // 景深：远=淡
    const w=(el.textContent||'').length*7.2+12, h=16;
    let prio=o.userData.prio||0, hl=false;
    if(hover){if(hoverSet.has(o.userData.nid)||(hoverFaceId&&o.userData.fid===hoverFaceId)){prio+=1e5;hl=true;}else prio-=1e3;}
    items.push({el,sx,sy,w,h,fade,prio,hl,base:o.userData.baseOp==null?1:o.userData.baseOp});}
  items.sort((a,b)=>b.prio-a.prio);
  const occ=[]; let shown=0; const MAX=44;
  for(const it of items){const{el,sx,sy,w,h}=it;
    const x0=sx-w/2,x1=sx+w/2,y0=sy-h/2,y1=sy+h/2;
    let ov=false;for(const r of occ){if(x0<r.x1&&x1>r.x0&&y0<r.y1&&y1>r.y0){ov=true;break;}}
    if(!it.hl&&(ov||shown>=MAX)){el.classList.add('hide');}
    else{el.classList.remove('hide');el.classList.toggle('hl',it.hl);
      let op=it.base*it.fade; if(hover&&!it.hl)op*=0.28;
      el.style.opacity=op; if(!it.hl){occ.push({x0,x1,y0,y1});shown++;}}}
  window.__persomeLabelHealth={total:labels3d.length,candidates:items.length,shown,camera:camera.position.toArray(),target:controls.target.toArray()};
}
// ── 爆炸成图动效：核心闪光 → 冲击波扩散 → 点涟漪式炸出(过冲 pop) → 镜头从近拉开揭全貌 ──
let spawnStart=0, spawning=false, camStart=null, camEnd=null, flashObj=null, shockObj=null;
const easeOut=t=>1-Math.pow(1-t,3);
const easeBack=t=>{const c1=2.4,c3=c1+1;return 1+c3*Math.pow(t-1,3)+c1*Math.pow(t-1,2);}; // 过冲后落定
function explode(){
  spawnStart=performance.now();spawning=true;controls.enabled=false; // 动效期间接管相机
  camEnd=camera.position.clone();camStart=camEnd.clone().multiplyScalar(0.38); // 从近处拉开
  flashObj=new THREE.Sprite(new THREE.SpriteMaterial({map:circleTex,color:0xffffff,transparent:true,opacity:0,depthTest:false,depthWrite:false,blending:THREE.AdditiveBlending}));
  flashObj.renderOrder=9;scene.add(flashObj);
  shockObj=new THREE.Mesh(new THREE.RingGeometry(0.5,0.68,72),new THREE.MeshBasicMaterial({color:0x9fc6ff,transparent:true,opacity:0,side:THREE.DoubleSide,depthWrite:false,blending:THREE.AdditiveBlending}));
  shockObj.rotation.x=-Math.PI/2;scene.add(shockObj);
}
function endSpawn(){spawning=false;controls.enabled=true;if(camEnd)camera.position.copy(camEnd);
  for(const sp of nodeSprites){const tgt=sp.userData.target;if(!tgt)continue;sp.position.copy(tgt);sp.scale.setScalar(sp.userData.baseScale||sp.scale.x);sp.material.opacity=sp.userData.baseOp==null?1:sp.userData.baseOp;
    const g=sp.userData.glow;if(g){g.position.copy(tgt);g.scale.setScalar(g.userData.baseScale);g.material.opacity=g.userData.baseOp;}}
  graph.traverse(o=>{o.visible=true;});labelRenderer.domElement.style.opacity=1;
  if(flashObj){scene.remove(flashObj);flashObj=null;}if(shockObj){scene.remove(shockObj);shockObj=null;}}
function stepSpawn(){
  const el=performance.now()-spawnStart, TOTAL=1800;
  if(camStart&&camEnd){camera.position.lerpVectors(camStart,camEnd,easeOut(Math.min(1,el/1450)));camera.lookAt(controls.target);}
  if(flashObj){flashObj.material.opacity=Math.max(0,(el<90?el/90:1-(el-90)/340))*0.95;flashObj.scale.setScalar(0.4+Math.min(1,el/430)*3);}
  if(shockObj){const s=Math.min(1,el/740);shockObj.scale.setScalar(0.3+s*7.5);shockObj.material.opacity=Math.max(0,1-s)*0.7;}
  for(const sp of nodeSprites){const tgt=sp.userData.target;if(!tgt)continue;
    const delay=Math.min(tgt.length()*80,560), lp=Math.max(0,Math.min(1,(el-delay)/770)), eb=Math.max(0.05,easeBack(lp)), op=Math.min(1,lp*1.7);
    sp.position.copy(tgt).multiplyScalar(easeOut(lp));                    // 位置缓出飞到位
    sp.scale.setScalar((sp.userData.baseScale||sp.scale.x)*eb);           // 大小过冲 pop
    sp.material.opacity=(sp.userData.baseOp==null?1:sp.userData.baseOp)*op;
    const g=sp.userData.glow;if(g){g.position.copy(sp.position);g.scale.setScalar(g.userData.baseScale*eb);g.material.opacity=g.userData.baseOp*op;}}
  const decoOn=el>560;
  graph.traverse(o=>{if(o!==graph&&!o.userData.label&&o.userData.kind!=='node'&&!o.userData.glow&&!o.isCSS2DObject)o.visible=decoOn;});
  labelRenderer.domElement.style.opacity=Math.max(0,Math.min(1,(el-920)/560));
  if(el>TOTAL)endSpawn();
}
function sampleRenderedPixels(){
  if(window.__persomeModelRender&&window.__persomeModelRender.lit>0)return;
  const gl=renderer.getContext(),w=gl.drawingBufferWidth,h=gl.drawingBufferHeight;
  let lit=0,checked=0;
  for(let yi=1;yi<10;yi++)for(let xi=1;xi<16;xi++){
    const p=new Uint8Array(4);
    gl.readPixels(Math.floor(w*xi/16),Math.floor(h*yi/10),1,1,gl.RGBA,gl.UNSIGNED_BYTE,p);
    checked++;if(p[0]+p[1]+p[2]>12)lit++;
  }
  window.__persomeModelRender={width:w,height:h,checked,lit};
}
function tick(){controls.update();
  if(spawning)stepSpawn(); else applyHover();
  cullLabels();
  renderer.render(scene,camera);sampleRenderedPixels();labelRenderer.render(scene,camera);requestAnimationFrame(tick);}
tick();
explode();  // 打开/刷新即播放「一点炸开成记忆图」入场动效（此处 spawnStart/spawning 已初始化）
window.addEventListener('resize',()=>{camera.aspect=view.clientWidth/view.clientHeight;camera.updateProjectionMatrix();renderer.setSize(view.clientWidth,view.clientHeight);labelRenderer.setSize(view.clientWidth,view.clientHeight);});
</script>
</body></html>
"""
