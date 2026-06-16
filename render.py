# -*- coding: utf-8 -*-
"""永劫无间战绩查询插件 - 图片渲染层。

生成水墨风格的战绩 HTML，交给框架的 html_render 渲染成图片。
背景图为插件内置的水墨人物画（img/naraka_bg.png），以 base64 内嵌。
常用英雄头像使用接口返回的 heroIcon URL 在线加载，失败时兜底为首字。
"""

import base64
import os
from html import escape

_BG_CACHE = None


def _bg_base64():
    """读取内置水墨背景图并转 base64（带缓存）。失败返回空串。"""
    global _BG_CACHE
    if _BG_CACHE is not None:
        return _BG_CACHE
    try:
        path = os.path.join(os.path.dirname(__file__), "img", "naraka_bg.png")
        with open(path, "rb") as f:
            _BG_CACHE = base64.b64encode(f.read()).decode()
    except Exception:
        _BG_CACHE = ""
    return _BG_CACHE


def _comma(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _hero_chip(name, cnt, icon):
    """单个常用英雄：圆形头像 + 名字 + 场数。头像加载失败显示首字。"""
    safe_name = escape(str(name))
    first = safe_name[:1]
    if icon:
        safe_icon = escape(str(icon))
        avatar = (
            f'<span class="ha">'
            f'<img src="{safe_icon}" onerror="this.style.display=\'none\';'
            f"this.parentNode.classList.add('noimg');\" />"
            f'<span class="ha-fb">{first}</span></span>'
        )
    else:
        avatar = f'<span class="ha noimg"><span class="ha-fb">{first}</span></span>'
    return (
        f'<div class="ht">{avatar}'
        f'<span class="ht-name">{safe_name}</span>'
        f"<b>{cnt}场</b></div>"
    )


def build_html(
    name,
    uid,
    tier_scores,
    cur_stats,
    cur_mode_name,
    valid_games,
    heroes,
    self_avg,
    mates,
    detail=None,
    scale=2.0,
):
    """构造完整战绩 HTML 字符串。scale 控制整体渲染倍数（清晰度）。"""
    bg = _bg_base64()
    safe_name = escape(str(name or "未知"))
    safe_uid = escape(str(uid or ""))

    # 段位名与分数
    grade_name = ""
    grade_score = None
    if cur_stats and isinstance(cur_stats.get("grade"), dict):
        g = cur_stats["grade"]
        grade_name = f"{g.get('gradeName', '')}{g.get('gradeLevel', '') or ''}".strip()
        grade_score = g.get("gradeScore")

    rk = tier_scores.get("排位", [0, 0, 0])
    tr = tier_scores.get("天人", [0, 0, 0])

    # 常用英雄
    hero_html = (
        "".join(_hero_chip(n, c, i) for n, c, i in heroes)
        or '<div class="ht-empty">暂无数据</div>'
    )

    # 队友
    if mates:
        mate_html = ""
        for m in mates:
            mate_html += (
                f'<div class="mate"><div class="mn">{escape(str(m["name"]))}'
                f'<span class="p">{m["plays"]}次</span></div>'
                f'<div class="ms">伤害 <b>{m["avg_damage"]:.0f}</b> · '
                f"治疗 <b>{m.get('avg_cure', 0):.0f}</b> · "
                f"振刀 <b>{m['avg_shock']:.1f}</b></div></div>"
            )
        mate_section = '<div class="st">常组队友</div>' + mate_html
    else:
        mate_section = ""

    # 详细战绩（逐场明细：名次/英雄/伤害/治疗/振刀）
    if detail:
        rows_html = ""
        for r in detail:
            rrank = escape(str(r.get("rank", "?")))
            rk_cls = "r1" if rrank == "1" else ("r3" if rrank in ("2", "3") else "")
            rows_html += (
                f'<div class="drow">'
                f'<span class="drk {rk_cls}">#{rrank}</span>'
                f'<span class="dhero">{escape(str(r.get("hero", "?")))}</span>'
                f'<span class="dval">伤 <b>{r.get("damage", 0)}</b></span>'
                f'<span class="dval">疗 <b>{r.get("cure", 0)}</b></span>'
                f'<span class="dval">刀 <b>{r.get("shock", 0)}</b></span>'
                f"</div>"
            )
        detail_section = (
            '<div class="st detail-title">详细战绩</div>'
            f'<div class="dlist">{rows_html}</div>'
        )
    else:
        detail_section = ""

    grade_line = f'<div class="grade">{escape(grade_name)}</div>' if grade_name else ""
    score_block = (
        f'<div class="sc">{_comma(grade_score)}</div><div class="scl">段位分数</div>'
        if grade_score is not None
        else ""
    )

    # 背景图作为 .card 的 background-image 铺满；失败则纯宣纸底
    if bg:
        bg_css = f"url('data:image/png;base64,{bg}')"
    else:
        bg_css = "none"

    _html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html {{ margin:0; padding:0; background-color:#f6f3ec; zoom:{scale}; }}
body {{ position:relative; width:1180px; margin:0; padding:0; min-height:900px;
  background-color:#f6f3ec; background-image:BGURL;
  background-size:cover; background-position:right center; background-repeat:no-repeat;
  font-family:"Noto Serif SC","Songti SC","Microsoft YaHei",serif; }}
.bg-fade {{ position:absolute; inset:0; z-index:1; pointer-events:none;
  background:linear-gradient(100deg, rgba(246,243,236,0.93) 0%, rgba(246,243,236,0.80) 45%,
    rgba(246,243,236,0.55) 70%, rgba(246,243,236,0.30) 100%); }}
.card {{ position:relative; width:1180px; min-height:900px;
  background:transparent; overflow:visible; z-index:2; }}
.ink-top {{ position:absolute; top:-80px; left:-60px; width:300px; height:240px;
  background:radial-gradient(ellipse, rgba(50,50,55,0.10), transparent 68%); filter:blur(3px); z-index:2; }}
.accent-line {{ position:absolute; top:0; left:0; width:40%; height:4px;
  background:linear-gradient(90deg,#9e2b25,#c0392b 40%,transparent); z-index:3; }}
.inner {{ position:relative; z-index:2; padding:32px 36px 36px;
  display:flex; gap:30px; align-items:flex-start; }}
.col-left {{ width:600px; flex-shrink:0; }}
.col-right {{ flex:1; padding-top:4px; }}
/* 详细战绩 */
.detail-title {{ margin-bottom:14px; }}
.dlist {{ display:flex; flex-direction:column; gap:7px; }}
.drow {{ display:flex; align-items:center; gap:10px;
  background:rgba(252,250,245,0.78); border:1px solid rgba(50,40,30,0.10);
  border-radius:8px; padding:9px 14px; font-size:13px; color:#3a332a; }}
.drow .drk {{ font-weight:800; color:#8a7a60; min-width:34px; font-size:13px; }}
.drow .drk.r1 {{ color:#c0392b; }}
.drow .drk.r3 {{ color:#d08a3d; }}
.drow .dhero {{ font-weight:700; color:#22201c; min-width:58px; }}
.drow .dval {{ color:#7a6e5e; font-size:12px; }}
.drow .dval b {{ color:#3a332a; font-weight:700; }}
.header {{ padding-bottom:16px; margin-bottom:20px; border-bottom:1px solid rgba(50,40,30,0.2); }}
.pname {{ font-size:30px; font-weight:700; color:#22201c; letter-spacing:1px; }}
.dot {{ display:inline-block; width:9px; height:9px; background:#c0392b; border-radius:50%;
  margin-right:11px; vertical-align:middle; }}
.uid {{ font-size:13px; color:#8a7f6d; margin-top:7px; letter-spacing:1px; margin-left:20px; }}
.scores {{ display:flex; gap:14px; margin-bottom:18px; }}
.sb {{ flex:1; background:rgba(252,250,245,0.82); border-radius:10px; padding:14px 16px;
  border:1px solid rgba(50,40,30,0.12); }}
.sb-label {{ font-size:14px; font-weight:700; color:#332d24; margin-bottom:10px;
  display:flex; align-items:center; gap:7px; }}
.sb-label::before {{ content:""; width:3px; height:14px; background:#7a6a50; }}
.sb.tr .sb-label::before {{ background:#c0392b; }}
.sb-row {{ display:flex; }}
.si {{ text-align:center; flex:1; }}
.si .v {{ font-size:20px; font-weight:700; color:#22201c; }}
.si .l {{ font-size:10px; color:#9c907c; margin-top:4px; }}
.cur {{ position:relative; border-radius:11px; padding:18px 22px; margin-bottom:22px;
  display:flex; align-items:center; justify-content:space-between; overflow:hidden;
  background:linear-gradient(120deg, rgba(34,30,26,0.94), rgba(28,25,21,0.88)); }}
.cur::after {{ content:""; position:absolute; top:-20px; right:30px; width:150px; height:150px;
  background:radial-gradient(circle, rgba(192,57,43,0.28), transparent 64%); filter:blur(6px); }}
.cl {{ position:relative; z-index:2; }}
.cl .mode {{ font-size:12px; color:#c6b9a4; }}
.cl .grade {{ font-size:25px; font-weight:700; color:#f3ecdf; margin-top:5px; letter-spacing:1px; }}
.cr {{ position:relative; z-index:2; text-align:right; }}
.cr .sc {{ font-size:36px; font-weight:800; color:#e8a23d; line-height:1;
  text-shadow:0 0 14px rgba(232,162,61,0.35); }}
.cr .scl {{ font-size:10px; color:#b0a48e; margin-top:5px; }}
.st {{ font-size:15px; color:#22201c; font-weight:700; margin-bottom:12px;
  display:flex; align-items:center; gap:9px; }}
.st::before {{ content:""; width:4px; height:16px; background:#c0392b; }}
.stats {{ display:flex; gap:9px; margin-bottom:22px; }}
.stat {{ flex:1; background:rgba(252,250,245,0.82); border-radius:10px; padding:14px 6px;
  text-align:center; border:1px solid rgba(50,40,30,0.12); }}
.stat .v {{ font-size:19px; font-weight:800; color:#22201c; }}
.stat .l {{ font-size:11px; color:#9c907c; margin-top:5px; }}
.heroes {{ margin-bottom:22px; }}
.htags {{ display:flex; gap:11px; flex-wrap:wrap; }}
.ht {{ display:flex; align-items:center; gap:8px;
  background:rgba(252,250,245,0.85); border:1px solid rgba(192,57,43,0.3);
  border-radius:24px; padding:5px 14px 5px 5px; font-size:13px; color:#5a4a40; }}
.ht .ha {{ position:relative; width:30px; height:30px; border-radius:50%; overflow:hidden;
  flex-shrink:0; background:#e7ddcd; display:inline-block; }}
.ht .ha img {{ width:100%; height:100%; object-fit:cover; position:relative; z-index:2; }}
.ht .ha-fb {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  font-size:13px; color:#8a7a60; font-weight:700; z-index:1; }}
.ht .ht-name {{ font-weight:600; }}
.ht b {{ color:#c0392b; }}
.ht-empty {{ font-size:13px; color:#9c907c; }}
.mate {{ background:rgba(252,250,245,0.82); border-radius:10px; padding:13px 18px;
  margin-bottom:9px; display:flex; align-items:center; justify-content:space-between;
  border:1px solid rgba(50,40,30,0.12); }}
.mn {{ font-size:14px; color:#22201c; font-weight:700; }}
.mn .p {{ font-size:11px; color:#9c907c; font-weight:400; margin-left:8px; }}
.ms {{ font-size:12px; color:#6a5e4e; }}
.ms b {{ color:#9e2b25; }}
.footer {{ text-align:center; margin-top:20px; font-size:11px; color:#a89c88; letter-spacing:3px; }}
</style></head><body>
<div class="bg-fade"></div>
<div class="card">
  <div class="ink-top"></div>
  <div class="accent-line"></div>
  <div style="position:relative; z-index:2; padding:32px 36px 0;">
    <div class="header">
      <div class="pname"><span class="dot"></span>{safe_name}</div>
      <div class="uid">UID: {safe_uid}</div>
    </div>
  </div>
  <div class="inner">
    <div class="col-left">
    <div class="scores">
      <div class="sb">
        <div class="sb-label">排位</div>
        <div class="sb-row">
          <div class="si"><div class="v">{rk[0]}</div><div class="l">单排</div></div>
          <div class="si"><div class="v">{rk[1]}</div><div class="l">双排</div></div>
          <div class="si"><div class="v">{rk[2]}</div><div class="l">三排</div></div>
        </div>
      </div>
      <div class="sb tr">
        <div class="sb-label">天人</div>
        <div class="sb-row">
          <div class="si"><div class="v">{tr[0]}</div><div class="l">单排</div></div>
          <div class="si"><div class="v">{tr[1]}</div><div class="l">双排</div></div>
          <div class="si"><div class="v">{tr[2]}</div><div class="l">三排</div></div>
        </div>
      </div>
    </div>
    <div class="cur">
      <div class="cl">
        <div class="mode">当前模式 · {escape(cur_mode_name)}</div>
        {grade_line}
      </div>
      <div class="cr">{score_block}</div>
    </div>
    <div class="st">最近战绩（有效对局 {valid_games}）</div>
    <div class="stats">
      <div class="stat"><div class="v">{self_avg["avg_damage"]:.0f}</div><div class="l">场均伤害</div></div>
      <div class="stat"><div class="v">{self_avg.get("avg_cure", 0):.0f}</div><div class="l">场均治疗</div></div>
      <div class="stat"><div class="v">{self_avg["avg_kill"]:.1f}</div><div class="l">场均击败</div></div>
      <div class="stat"><div class="v">{self_avg["avg_shock"]:.1f}</div><div class="l">场均振刀</div></div>
    </div>
    <div class="heroes">
      <div class="st">常用英雄</div>
      <div class="htags">{hero_html}</div>
    </div>
    {mate_section}
    </div>
    <div class="col-right">
      {detail_section}
    </div>
  </div>
  <div class="footer" style="position:relative; z-index:2;">N A R A K A　B L A D E P O I N T</div>
</div></body></html>"""
    return _html.replace("BGURL", bg_css)
