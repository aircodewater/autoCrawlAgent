"""从爬取状态生成 URL 导航树 JSON（与历史 utoronto_*_url_tree.json 结构兼容）及可浏览器打开的可视化 HTML。"""

from __future__ import annotations

import json
from html import escape as html_escape
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set, Tuple
from urllib.parse import urlparse


def _norm_url(u: str) -> str:
    """与 crawler.graph._norm_url 一致，避免循环依赖。"""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        if "://" not in u and u.startswith("//"):
            u = "https:" + u
        elif "://" not in u:
            u = "https://" + u.lstrip("/")
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        path = path or "/"
        return f"{scheme}://{netloc}{path}".lower()
    except Exception:
        return (u or "").split("#")[0].lower().rstrip("/")


def _adjacency(edges: List[Dict[str, str]]) -> Dict[str, List[str]]:
    adj: Dict[str, List[str]] = {}
    for e in edges:
        p, c = (e.get("parent") or "").strip(), (e.get("child") or "").strip()
        if not p or not c or p == c:
            continue
        adj.setdefault(p, [])
        if c not in adj[p]:
            adj[p].append(c)
    return adj


def build_nested_tree(
    start_url: str,
    edges: List[Dict[str, str]],
    *,
    display: Mapping[str, str],
) -> Dict[str, Any]:
    """构建 { url, children } 嵌套树；遇环则不再向下展开。"""
    adj = _adjacency(edges)
    root = _norm_url(start_url)
    if not root:
        return {"url": (start_url or "").strip(), "children": []}

    def shown(u: str) -> str:
        return (display.get(u) or u).strip() or u

    def walk(u: str, ancestors: Set[str]) -> Dict[str, Any]:
        if u in ancestors:
            return {"url": shown(u), "children": []}
        kids = adj.get(u, [])
        nxt = ancestors | {u}
        return {"url": shown(u), "children": [walk(v, nxt) for v in kids]}

    return walk(root, set())


def node_updated_fields_json(
    url_node_topics: Mapping[str, List[str]],
    display: Mapping[str, str],
) -> Dict[str, List[str]]:
    """规范化键 → 展示用 URL 字符串。"""
    out: Dict[str, List[str]] = {}
    for norm, topics in (url_node_topics or {}).items():
        if not topics:
            continue
        key = (display.get(norm) or norm).strip() or norm
        out[key] = list(topics)
    return out


def build_url_fields_document(
    start_url: str,
    url_node_topics: Mapping[str, List[str]],
    url_display: Mapping[str, str],
) -> Dict[str, List[str]]:
    """URL → 在该页首次写入或更新过的字段名列表（与 topic 文件一致）。"""
    display = dict(url_display or {})
    root_n = _norm_url(start_url)
    if root_n and start_url.strip():
        display.setdefault(root_n, start_url.strip())

    by_url = node_updated_fields_json(url_node_topics, display)
    return {url: list(fields) for url, fields in sorted(by_url.items())}


def write_url_fields_by_page(
    *,
    out_dir: Path,
    stem: str,
    start_url: str,
    url_node_topics: Mapping[str, List[str]],
    url_display: Mapping[str, str],
) -> Path:
    """写入 ``{{stem}}_url_fields.json``：扁平 JSON，键为 URL、值为字段名列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = build_url_fields_document(start_url, url_node_topics, url_display)
    json_path = (out_dir / f"{stem}_url_fields.json").resolve()
    json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_path


def collect_graph_nodes_edges(
    start_url: str,
    edges: List[Dict[str, str]],
    url_node_topics: Mapping[str, List[str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """vis-network 用节点/边（id 为规范化 URL）；保留供调试或其它用途。"""
    root = _norm_url(start_url)
    norms: Set[str] = set()
    if root:
        norms.add(root)
    for e in edges:
        p, c = (e.get("parent") or "").strip(), (e.get("child") or "").strip()
        if p:
            norms.add(p)
        if c:
            norms.add(c)
    for k in url_node_topics:
        nk = _norm_url(k)
        if nk:
            norms.add(nk)

    nodes: List[Dict[str, Any]] = []
    for n in sorted(norms):
        label = n
        if len(label) > 72:
            label = label[:35] + "…" + label[-30:]
        nodes.append({"id": n, "label": label, "title": n})

    vis_edges: List[Dict[str, Any]] = []
    seen_e: Set[Tuple[str, str]] = set()
    for e in edges:
        p, c = (e.get("parent") or "").strip(), (e.get("child") or "").strip()
        if not p or not c or p == c:
            continue
        key = (p, c)
        if key in seen_e:
            continue
        seen_e.add(key)
        vis_edges.append({"from": p, "to": c})
    return nodes, vis_edges


def _topics_for_norm(
    n: str, display: Mapping[str, str], node_fields: Mapping[str, List[str]]
) -> List[str]:
    disp = (display.get(n) or n).strip() or n
    if disp in node_fields:
        return list(node_fields[disp])
    if n in node_fields:
        return list(node_fields[n])
    for k, v in node_fields.items():
        if _norm_url(k) == n:
            return list(v)
    return []


def build_vis_nodes_edges_int_ids(
    start_url: str,
    flat_edges: List[Dict[str, str]],
    node_fields: Mapping[str, List[str]],
    display: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, int]]]:
    """与历史 ``*_url_tree_viz.html`` 一致：节点为整数 id，label 内含 URL + 本页字段列表。"""
    seen: Set[str] = set()
    norms: List[str] = []

    def add_norm(x: str) -> None:
        x = (x or "").strip()
        if not x or x in seen:
            return
        seen.add(x)
        norms.append(x)

    rn = _norm_url(start_url)
    if rn:
        add_norm(rn)
    for e in flat_edges:
        add_norm((e.get("parent") or "").strip())
        add_norm((e.get("child") or "").strip())
    for key in node_fields:
        add_norm(_norm_url(key))

    norm_to_id = {n: i for i, n in enumerate(norms)}
    nodes_out: List[Dict[str, Any]] = []
    for i, n in enumerate(norms):
        disp = (display.get(n) or n).strip() or n
        topics = _topics_for_norm(n, display, node_fields)
        if topics:
            body = "— 本页提取字段 —\n" + "\n".join(topics)
        else:
            body = "（本页无字段合并记录）"
        label = disp + "\n\n" + body
        nodes_out.append({"id": i, "title": disp, "label": label, "urlKey": disp})

    edges_out: List[Dict[str, int]] = []
    seen_e: Set[Tuple[str, str]] = set()
    for e in flat_edges:
        p, c = (e.get("parent") or "").strip(), (e.get("child") or "").strip()
        if p not in norm_to_id or c not in norm_to_id:
            continue
        key = (p, c)
        if key in seen_e:
            continue
        seen_e.add(key)
        edges_out.append({"from": norm_to_id[p], "to": norm_to_id[c]})
    return nodes_out, edges_out


def _json_for_html_script(obj: Any) -> str:
    """嵌入 ``<script type="application/json">`` 时避免 ``</script>`` 截断。"""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def render_url_tree_html_bundle(
    *,
    stem: str,
    start_url: str,
    node_fields: Dict[str, List[str]],
    flat_edges: List[Dict[str, str]],
    display: Mapping[str, str],
) -> str:
    """布局与 ``utoronto_*_url_tree_viz.html`` 对齐：顶栏 + 全屏图 + 底部详情 + 导出 PNG。"""
    nodes_out, edges_out = build_vis_nodes_edges_int_ids(
        start_url, flat_edges, node_fields, display
    )
    nodes_json = _json_for_html_script(nodes_out)
    edges_json = _json_for_html_script(edges_out)
    title = html_escape(f"爬取导航树 · {stem}")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <script type="text/javascript" src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, sans-serif; }}
    #toolbar {{
      padding: 10px 14px;
      border-bottom: 1px solid #ddd;
      background: #f8f9fa;
      font-size: 14px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }}
    #toolbar button {{
      padding: 6px 12px;
      font-size: 13px;
      cursor: pointer;
      border: 1px solid #cbd5e0;
      border-radius: 6px;
      background: #fff;
    }}
    #toolbar button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    #toolbar label {{ font-size: 13px; color: #4a5568; }}
    #mynetwork {{
      width: 100%;
      height: calc(100vh - 52px);
      border: none;
      background: #fff;
    }}
  </style>
</head>
<body>
  <div id="toolbar">
    <strong>{title}</strong>
    <span style="color:#718096;">· 可拖拽缩放 · 点击节点在下方查看网址</span>
    <label>导出清晰度 <select id="exportScale"><option value="2" selected>2×</option><option value="3">3×</option><option value="4">4×</option></select></label>
    <button type="button" id="btnExportPng">导出 PNG 大图</button>
  </div>
  <div id="mynetwork"></div>
  <div id="detail" style="padding:8px 14px;font-size:13px;color:#333;border-top:1px solid #eee;max-height:120px;overflow:auto;"></div>
  <script type="application/json" id="ndata">{nodes_json}</script>
  <script type="application/json" id="edata">{edges_json}</script>
  <script type="text/javascript">
    const nodes = new vis.DataSet(JSON.parse(document.getElementById("ndata").textContent));
    const edges = new vis.DataSet(JSON.parse(document.getElementById("edata").textContent));
    const container = document.getElementById("mynetwork");
    const detail = document.getElementById("detail");
    const data = {{ nodes: nodes, edges: edges }};
    const options = {{
      layout: {{
        hierarchical: {{
          enabled: true,
          direction: "UD",
          sortMethod: "directed",
          levelSeparation: 280,
          nodeSpacing: 380,
          treeSpacing: 280,
          blockShifting: true,
          edgeMinimization: true,
          parentCentralization: true,
          shakeTowards: "leaves",
        }},
      }},
      physics: false,
      interaction: {{ hover: true, zoomView: true, dragView: true }},
      nodes: {{
        shape: "box",
        margin: 20,
        font: {{ multi: true, align: "left", size: 12, face: "system-ui, Segoe UI, sans-serif" }},
        color: {{ background: "#e8f4fc", border: "#2b6cb0", highlight: {{ background: "#bee3f8", border: "#2c5282" }} }},
        borderWidth: 1,
      }},
      edges: {{
        arrows: "to",
        color: {{ color: "#718096" }},
        smooth: {{ type: "cubicBezier", forceDirection: "vertical", roundness: 0.35 }},
      }},
    }};
    const network = new vis.Network(container, data, options);
    setTimeout(function () {{
      network.fit({{ animation: false, padding: 120 }});
    }}, 150);
    network.on("click", function (p) {{
      if (p.nodes.length === 0) {{ detail.textContent = ""; return; }}
      const id = p.nodes[0];
      const n = nodes.get(id);
      detail.textContent = n && n.title ? n.title : "";
    }});

    function exportLargePng() {{
      const btn = document.getElementById("btnExportPng");
      const scaleSel = document.getElementById("exportScale");
      const scale = Math.max(1, Math.min(4, parseInt(scaleSel.value, 10) || 2));
      btn.disabled = true;
      const prevStyleW = container.style.width;
      const prevStyleH = container.style.height;
      const prevClientW = container.clientWidth;
      const prevClientH = container.clientHeight;
      const vw = Math.max(4000, Math.floor(prevClientW * 1.2));
      const vh = Math.max(3400, Math.floor(prevClientH * 1.5));
      container.style.width = vw + "px";
      container.style.height = vh + "px";
      network.setSize(vw, vh);
      network.fit({{ animation: false, padding: 100 }});

      const finish = function () {{
        const src = container.querySelector("canvas");
        if (!src) {{
          alert("未找到画布，导出失败。");
        }} else {{
          const w = src.width;
          const h = src.height;
          const out = document.createElement("canvas");
          out.width = Math.floor(w * scale);
          out.height = Math.floor(h * scale);
          const ctx = out.getContext("2d");
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, out.width, out.height);
          ctx.imageSmoothingEnabled = true;
          ctx.imageSmoothingQuality = "high";
          ctx.drawImage(src, 0, 0, w, h, 0, 0, out.width, out.height);
          out.toBlob(function (blob) {{
            if (!blob) {{
              alert("生成图片失败。");
            }} else {{
              const u = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = u;
              a.download = "crawl_url_tree_" + scale + "x.png";
              document.body.appendChild(a);
              a.click();
              a.remove();
              setTimeout(function () {{ URL.revokeObjectURL(u); }}, 2000);
            }}
          }}, "image/png");
        }}
        container.style.width = prevStyleW;
        container.style.height = prevStyleH;
        network.setSize(prevClientW, prevClientH);
        network.fit({{ animation: false, padding: 40 }});
        btn.disabled = false;
      }};

      requestAnimationFrame(function () {{
        requestAnimationFrame(function () {{
          setTimeout(finish, 120);
        }});
      }});
    }}

    document.getElementById("btnExportPng").addEventListener("click", exportLargePng);
  </script>
</body>
</html>
"""


def write_url_tree_artifacts(
    *,
    out_dir: Path,
    stem: str,
    start_url: str,
    url_nav_edges: List[Dict[str, str]],
    url_node_topics: Mapping[str, List[str]],
    url_display: Mapping[str, str],
) -> Tuple[Path, Path]:
    """写入 ``{{stem}}_url_tree.json`` 与 ``{{stem}}_url_tree.html``。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    display = dict(url_display or {})
    root_n = _norm_url(start_url)
    if root_n and start_url.strip():
        display.setdefault(root_n, start_url.strip())

    tree = build_nested_tree(start_url, url_nav_edges, display=display)
    node_fields = node_updated_fields_json(url_node_topics, display)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc: Dict[str, Any] = {
        "start_url": (display.get(root_n) if root_n else None) or start_url.strip(),
        "generated_at": generated_at,
        "tree": tree,
        "node_updated_fields": node_fields,
    }
    json_path = (out_dir / f"{stem}_url_tree.json").resolve()
    html_path = (out_dir / f"{stem}_url_tree.html").resolve()
    json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    html_doc = render_url_tree_html_bundle(
        stem=stem,
        start_url=start_url.strip(),
        node_fields=node_fields,
        flat_edges=url_nav_edges,
        display=display,
    )
    html_path.write_text(html_doc, encoding="utf-8")
    return json_path, html_path
