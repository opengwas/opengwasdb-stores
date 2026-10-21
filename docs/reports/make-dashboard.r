## Generate a self-contained, offline store-prioritisation dashboard.
##
## Reads the per-store summary and emits a single HTML file with one draggable
## card per candidate store. Drag cards between the Unassigned / High / Low /
## Discard columns; each column tallies its store count, analysis count, and
## estimated storage. The layout persists in the browser (localStorage) and the
## prioritisation exports to CSV.
##
## Input : resources/data/derived/store-candidates-summary.tsv
## Output: resources/data/derived/store-prioritisation-dashboard.html
##
## Regenerate after re-running ebi-studies.r; the store data is embedded, so the
## file works offline with no server.

library(data.table)
library(jsonlite)

derived_dir <- "resources/data/derived"
summary_path <- file.path(derived_dir, "store-candidates-summary.tsv")
if (!file.exists(summary_path)) {
  stop("Run `Rscript resources/scripts/ebi-studies.r` first to build ", summary_path)
}

s <- fread(summary_path, sep = "\t", na.strings = "")

cards <- s[, .(
  store_key,
  store_type,
  molecular_type = fifelse(is.na(molecular_type), "", molecular_type),
  ancestry_group,
  n_analyses,
  est_gb = est_completed_size_gb,
  hit_prop = prop_with_gwas_hit,
  median_n = median_sample_size,
  n_case_control,
  n_quantitative,
  pubmed_id,
  first_author = fifelse(is.na(first_author), "", first_author),
  study_title = fifelse(is.na(study_title), "", study_title),
  needs_review,
  review_reason = fifelse(is.na(review_reason), "", review_reason)
)]
setorder(cards, -est_gb)

data_json <- toJSON(cards, dataframe = "rows", auto_unbox = TRUE, na = "null")
generated <- format(Sys.time(), "%Y-%m-%d %H:%M")

template <- r"---(<style>
  /* Cool, clinical neutrals biased toward the blue data accent. */
  :root{
    --bg:#f3f4f6; --surface:#fbfcfd; --surface-2:#eceef1; --border:#d8dbe0;
    --ink:#0d0f12; --ink-2:#4d5159; --ink-3:#868b94;
    --dense:#2a78d6; --ragged:#1baf7a; --hybrid:#eda100;
    --high:#008300; --low:#eda100; --discard:#e34948;
    --shadow:0 1px 2px rgba(20,24,33,.08),0 2px 8px rgba(20,24,33,.06);
  }
  :root[data-theme="light"]{
    --bg:#f3f4f6; --surface:#fbfcfd; --surface-2:#eceef1; --border:#d8dbe0;
    --ink:#0d0f12; --ink-2:#4d5159; --ink-3:#868b94;
    --dense:#2a78d6; --ragged:#1baf7a; --hybrid:#eda100;
    --high:#008300; --low:#eda100; --discard:#e34948;
    --shadow:0 1px 2px rgba(20,24,33,.08),0 2px 8px rgba(20,24,33,.06);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1113; --surface:#1a1d21; --surface-2:#22262b; --border:#343a42;
      --ink:#eef1f4; --ink-2:#b9bec7; --ink-3:#7f858e;
      --dense:#3987e5; --ragged:#199e70; --hybrid:#c98500;
      --high:#3ea63e; --low:#c98500; --discard:#e66767;
      --shadow:0 1px 2px rgba(0,0,0,.4),0 2px 10px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"]{
    --bg:#0f1113; --surface:#1a1d21; --surface-2:#22262b; --border:#343a42;
    --ink:#eef1f4; --ink-2:#b9bec7; --ink-3:#7f858e;
    --dense:#3987e5; --ragged:#199e70; --hybrid:#c98500;
    --high:#3ea63e; --low:#c98500; --discard:#e66767;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 2px 10px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
  header{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--border);
    padding:12px 18px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;box-shadow:var(--shadow)}
  header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:-.01em}
  .grow{flex:1 1 auto}
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  input[type=search],select{font:inherit;padding:6px 10px;border:1px solid var(--border);border-radius:8px;
    background:var(--bg);color:var(--ink)}
  input[type=search]{min-width:190px}
  button{font:inherit;font-weight:550;padding:6px 12px;border:1px solid var(--border);border-radius:8px;
    background:var(--surface-2);color:var(--ink);cursor:pointer}
  button.primary{background:var(--dense);border-color:transparent;color:#fff}
  button:hover{filter:brightness(1.05)}
  .board{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:12px;padding:14px 18px;align-items:start}
  @media (max-width:1000px){.board{grid-template-columns:1fr 1fr}}
  @media (max-width:640px){.board{grid-template-columns:1fr}}
  .col{background:var(--surface);border:1px solid var(--border);border-radius:12px;display:flex;flex-direction:column;
    min-height:120px}
  .col-head{padding:10px 12px;border-bottom:1px solid var(--border);position:sticky;top:64px;background:var(--surface);
    border-radius:12px 12px 0 0}
  .col-title{display:flex;align-items:center;gap:8px;font-weight:650;font-size:13px}
  .dot{width:10px;height:10px;border-radius:50%}
  .col[data-col=unassigned] .dot{background:var(--ink-3)}
  .col[data-col=high] .dot{background:var(--high)}
  .col[data-col=low] .dot{background:var(--low)}
  .col[data-col=discard] .dot{background:var(--discard)}
  .tiles{display:flex;gap:10px;margin-top:8px}
  .tile{flex:1;background:var(--surface-2);border-radius:8px;padding:6px 8px;text-align:center}
  .tile b{display:block;font-size:15px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
  .tile span{font-size:10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.04em}
  .drop{padding:10px;display:flex;flex-direction:column;gap:9px;flex:1;min-height:60px;transition:background .12s}
  .drop.over{background:color-mix(in srgb,var(--dense) 12%,transparent)}
  .card{background:var(--surface);border:1px solid var(--border);border-left-width:4px;border-radius:10px;
    padding:9px 11px;box-shadow:var(--shadow);cursor:grab;user-select:none}
  .card:active{cursor:grabbing}
  .card.dragging{opacity:.45}
  .card[data-type=dense]{border-left-color:var(--dense)}
  .card[data-type=ragged]{border-left-color:var(--ragged)}
  .card[data-type=hybrid]{border-left-color:var(--hybrid)}
  .card h3{margin:0 0 2px;font-size:13px;font-weight:620;letter-spacing:-.01em;display:flex;gap:6px;align-items:center}
  .card h3 .ttl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pmid{color:var(--dense);text-decoration:none;font-weight:600}
  .pmid:hover{text-decoration:underline}
  .pmid:focus-visible{outline:2px solid var(--dense);outline-offset:2px;border-radius:3px}
  .badge{font-size:10px;font-weight:650;padding:1px 6px;border-radius:20px;color:#fff;text-transform:uppercase;letter-spacing:.03em}
  .badge.dense{background:var(--dense)} .badge.ragged{background:var(--ragged)} .badge.hybrid{background:var(--hybrid)}
  .anc{font-size:10px;font-weight:600;padding:1px 7px;border-radius:20px;border:1px solid var(--border);
    background:var(--surface-2);color:var(--ink-2);white-space:nowrap}
  .flag{margin-left:auto;color:var(--discard);font-size:11px;font-weight:700;cursor:help}
  .sub{color:var(--ink-3);font-size:11px;margin:0 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .stats{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:11.5px;color:var(--ink-2);font-variant-numeric:tabular-nums}
  .stats b{color:var(--ink);font-weight:620}
  .empty{color:var(--ink-3);font-size:12px;text-align:center;padding:14px 0;border:1px dashed var(--border);border-radius:8px}
  footer{padding:8px 18px 20px;color:var(--ink-3);font-size:11px}
</style>
<header>
  <h1>OpenGWASDB store prioritisation</h1>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by name / trait / author…">
    <select id="type"><option value="">All types</option><option>dense</option><option>ragged</option><option>hybrid</option></select>
    <select id="sort">
      <option value="est_gb">Sort: size ↓</option>
      <option value="n_analyses">Sort: analyses ↓</option>
      <option value="hit_prop">Sort: hit rate ↓</option>
      <option value="median_n">Sort: sample size ↓</option>
    </select>
  </div>
  <div class="grow"></div>
  <div class="controls">
    <button id="reset">Reset</button>
    <button id="export" class="primary">Export CSV</button>
  </div>
</header>

<div class="board" id="board"></div>
<footer>Generated @@GENERATED@@ · drag cards between columns · layout saved in this browser · storage estimates are modelled (see ebi-studies.r)</footer>

<script>
const STORES = @@DATA@@;
const COLS = [
  {id:"unassigned", name:"Unassigned"},
  {id:"high", name:"High priority"},
  {id:"low", name:"Low priority"},
  {id:"discard", name:"Discard"}
];
const LSKEY = "ogwas-store-prio-v1";
const byKey = Object.fromEntries(STORES.map(s => [s.store_key, s]));

let assign = {};
try { assign = JSON.parse(localStorage.getItem(LSKEY)) || {}; } catch(e){ assign = {}; }
STORES.forEach(s => { s.col = assign[s.store_key] || "unassigned"; });

const fmtGB = g => g == null ? "–" : (g >= 100 ? Math.round(g) : g.toFixed(1)) + " GB";
const fmtInt = n => n == null ? "–" : n.toLocaleString();

function cardTitle(s){
  if (s.store_type === "hybrid") return "Hybrid pool";
  return s.first_author || s.store_key;
}
function pmidLink(s){
  if (!s.pubmed_id) return "";
  return ' · <a class="pmid" draggable="false" target="_blank" rel="noopener" ' +
    'href="https://pubmed.ncbi.nlm.nih.gov/' + s.pubmed_id + '/">' + s.pubmed_id + '</a>';
}

function saveAssign(){
  assign = {};
  STORES.forEach(s => { if (s.col !== "unassigned") assign[s.store_key] = s.col; });
  localStorage.setItem(LSKEY, JSON.stringify(assign));
}

function buildBoard(){
  const board = document.getElementById("board");
  board.innerHTML = "";
  COLS.forEach(c => {
    const col = document.createElement("div");
    col.className = "col"; col.dataset.col = c.id;
    col.innerHTML =
      '<div class="col-head"><div class="col-title"><span class="dot"></span>' + c.name + '</div>' +
      '<div class="tiles">' +
        '<div class="tile"><b data-t="count">0</b><span>stores</span></div>' +
        '<div class="tile"><b data-t="analyses">0</b><span>analyses</span></div>' +
        '<div class="tile"><b data-t="gb">0</b><span>est. GB</span></div>' +
      '</div></div>' +
      '<div class="drop" data-col="' + c.id + '"></div>';
    board.appendChild(col);
    const drop = col.querySelector(".drop");
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", e => {
      e.preventDefault(); drop.classList.remove("over");
      const key = e.dataTransfer.getData("text/plain");
      if (byKey[key]) { byKey[key].col = c.id; saveAssign(); render(); }
    });
  });
}

function render(){
  const q = document.getElementById("q").value.trim().toLowerCase();
  const typeF = document.getElementById("type").value;
  const sortK = document.getElementById("sort").value;
  const matches = s =>
    (!typeF || s.store_type === typeF) &&
    (!q || (s.store_key + " " + s.study_title + " " + s.first_author + " " + s.molecular_type + " " + s.ancestry_group).toLowerCase().includes(q));

  COLS.forEach(c => {
    const drop = document.querySelector('.drop[data-col="' + c.id + '"]');
    drop.innerHTML = "";
    const inCol = STORES.filter(s => s.col === c.id);
    const num = v => (v == null ? -1 : v);
    const shown = inCol.filter(matches).sort((a,b) => num(b[sortK]) - num(a[sortK]));
    shown.forEach(s => drop.appendChild(makeCard(s)));
    if (shown.length === 0) {
      const e = document.createElement("div");
      e.className = "empty";
      e.textContent = inCol.length ? "no matches in filter" : "drop stores here";
      drop.appendChild(e);
    }
    const col = drop.closest(".col");
    col.querySelector('[data-t=count]').textContent = fmtInt(inCol.length);
    col.querySelector('[data-t=analyses]').textContent = fmtInt(inCol.reduce((a,s)=>a+(s.n_analyses||0),0));
    col.querySelector('[data-t=gb]').textContent = fmtGB(inCol.reduce((a,s)=>a+(s.est_gb||0),0));
  });
}

function makeCard(s){
  const el = document.createElement("div");
  el.className = "card"; el.dataset.type = s.store_type; el.draggable = true;
  const flag = s.needs_review ? '<span class="flag" title="' + s.review_reason + '">⚑ review</span>' : "";
  const mol = s.molecular_type ? " · " + s.molecular_type : "";
  const anc = s.ancestry_group ? '<span class="anc">' + s.ancestry_group + '</span>' : "";
  el.innerHTML =
    '<h3><span class="ttl">' + cardTitle(s) + pmidLink(s) + '</span>' +
      '<span class="badge ' + s.store_type + '">' + s.store_type + '</span>' + anc + flag + '</h3>' +
    '<p class="sub">' + (s.study_title || s.store_key) + '</p>' +
    '<div class="stats">' +
      '<span><b>' + fmtInt(s.n_analyses) + '</b> analyses' + mol + '</span>' +
      '<span><b>' + fmtGB(s.est_gb) + '</b></span>' +
      '<span>hit <b>' + (s.hit_prop == null ? "–" : (s.hit_prop*100).toFixed(0) + "%") + '</b></span>' +
      '<span>N <b>' + fmtInt(s.median_n) + '</b></span>' +
    '</div>';
  el.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", s.store_key); el.classList.add("dragging"); });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

function exportCSV(){
  const cols = ["store_key","priority","store_type","molecular_type","ancestry_group",
                "n_analyses","est_gb","hit_prop","median_n","pubmed_id","needs_review","review_reason"];
  const esc = v => { v = (v==null?"":String(v)); return /[",\n]/.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; };
  const rows = [cols.join(",")];
  STORES.slice().sort((a,b)=>a.col.localeCompare(b.col)||(b.est_gb||0)-(a.est_gb||0)).forEach(s => {
    rows.push([s.store_key,s.col,s.store_type,s.molecular_type,s.ancestry_group,
      s.n_analyses,s.est_gb,s.hit_prop,s.median_n,s.pubmed_id,s.needs_review,s.review_reason].map(esc).join(","));
  });
  const blob = new Blob([rows.join("\n")], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "store-prioritisation.csv";
  a.click(); URL.revokeObjectURL(a.href);
}

document.getElementById("q").addEventListener("input", render);
document.getElementById("type").addEventListener("change", render);
document.getElementById("sort").addEventListener("change", render);
document.getElementById("export").addEventListener("click", exportCSV);
document.getElementById("reset").addEventListener("click", () => {
  if (!confirm("Move all stores back to Unassigned?")) return;
  STORES.forEach(s => s.col = "unassigned"); saveAssign(); render();
});

buildBoard();
render();
</script>
)---"

parts <- strsplit(template, "@@DATA@@", fixed = TRUE)[[1]]
inner <- paste0(parts[1], data_json, parts[2])
inner <- sub("@@GENERATED@@", generated, inner, fixed = TRUE)

# Standalone document (open directly in a browser). The template is body-inner
# only -- <style>, markup, and <script> -- so the same fragment could also be
# dropped into another host page; here it is wrapped into a full HTML file.
html <- paste0(
  "<!doctype html>\n<html lang=\"en\">\n<head>\n",
  "<meta charset=\"utf-8\">\n",
  "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n",
  "<title>OpenGWASDB store prioritisation</title>\n</head>\n<body>\n",
  inner, "\n</body>\n</html>\n")

out_path <- file.path(derived_dir, "store-prioritisation-dashboard.html")
writeLines(html, out_path)
cat(sprintf("Wrote %s (%d store cards).\n", out_path, nrow(cards)))
