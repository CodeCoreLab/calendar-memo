"""前端 XSS 转义静态检查（回归守护）。

用法：.venv/Scripts/python.exe tests_xss_static.py

做什么
------
1. 扫描 web/*.html 的 innerHTML 赋值点，按「是否拼接」分类
2. 对每个拼接点，判断右侧是否出现 esc()（或已被 esc 的 helper）
3. 未出现的，检查该行上方 3 行内是否有白名单注释（说明数据来源是数字/规范化日期）
4. 额外检查内联 script 的 JS 语法可编译

为什么需要它
------------
2026-10-29 全量审计确认现存 143 处 innerHTML 全部安全，但「全部安全」靠的是
逐条人工判断。那次判断的结论必须落成机检，否则下一次加渲染点就没人知道要 esc。

判定规则（刻意的保守设计）
--------------------------
- 多行拼接：以「语句为单位」而不是「行」判断，避免把多行语句的前半行误报
- 未找到 esc 且无白名单注释 → FAIL
- 白名单注释必须是紧邻上一行，且含 `已转义` / `Number` / `常量` / `静态文案`
  等词之一 —— 强制作者写下「为什么安全」，比强制写 esc() 更能防漏
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

# 允许的「无 esc 但安全」理由关键词（注释里必须出现其一）
SAFE_REASON = ("已转义", "Number", "数字", "规范化", "服务端", "白名单",
               "常量", "静态文案", "无变量", "无用户输入", "硬编码")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


def logical_statements(lines):
    """把「以运算符结尾」的行续到下一行，返回 [(起始行号, 完整文本)]。

    用括号配平做语句边界不可靠 —— 实测会把整段函数体吞成一条语句，
    产生大片误报。这里改用一个更贴近 JS 写作习惯的判据：
    **只有当上一行以 + 或 ? 或 : 或 ( 结尾时才续行**。
    跨行字符串拼接一定是这种形态，而函数/块定义一定不是。
    """
    out = []
    buf, start = "", 0
    for i, ln in enumerate(lines, 1):
        if not buf:
            start = i
        buf += ln.rstrip("\n") + "\n"
        stripped = ln.rstrip()
        cont = bool(re.search(r"[+?:,(]\s*$", stripped)) or stripped.endswith("\\")
        if not cont:
            out.append((start, buf.strip()))
            buf = ""
    if buf.strip():
        out.append((start, buf.strip()))
    return out


def has_esc(text):
    return "esc(" in text


def nearby_reason(lines, start_line):
    """语句起始行往上 3 行里是否有白名单理由注释。

    语句是多行时，注释写在语句第一行之前 —— start_line 已经是语句首行，
    所以只要从它往上找即可，不会被续行隔开。
    """
    for back in range(1, 4):
        idx = start_line - 1 - back
        if idx < 0:
            break
        txt = lines[idx].strip()
        if not txt:
            continue
        if txt.startswith("//") and any(k in txt for k in SAFE_REASON):
            return txt
        # 遇到非注释代码就停止（注释必须紧邻语句）
        if not txt.startswith("//"):
            break
    return None


def scan_file(path):
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    n_total = len(re.findall(r"\.innerHTML\s*=", src))

    stmts = logical_statements(lines)
    # 判据必须是「.innerHTML =」赋值 —— 只写 .innerHTML 会命中 HTML 里的
    # id="hlFoot" 这类标记，把静态标签当成渲染点（2026-10-29 实测踩到）
    concat_stmts = [(ln, t) for ln, t in stmts
                    if re.search(r"\.innerHTML\s*=", t)
                    and "+" in t.split(".innerHTML", 1)[-1]]

    ok_esc, ok_reason, bad = [], [], []
    for ln, text in concat_stmts:
        rhs = text.split(".innerHTML =", 1)[-1]
        if has_esc(rhs):
            ok_esc.append(ln)
        elif nearby_reason(lines, ln):
            ok_reason.append(ln)
        else:
            bad.append((ln, text[:120]))

    return n_total, len(concat_stmts), len(ok_esc), len(ok_reason), bad


def main():
    print("=" * 70)
    print("前端 XSS 转义静态检查")
    print("=" * 70)

    targets = [f for f in sorted(os.listdir(WEB)) if f.endswith(".html")]
    print(f"待检文件: {', '.join(targets)}\n")

    all_bad = []
    for f in targets:
        path = os.path.join(WEB, f)
        n_total, n_concat, n_esc, n_reason, bad = scan_file(path)
        print(f"── {f} ──")
        print(f"   innerHTML 赋值点: {n_total}")
        print(f"   其中含拼接:       {n_concat}")
        print(f"   显式 esc():       {n_esc}")
        print(f"   有白名单理由注释: {n_reason}")
        check(f"{f} 无未转义拼接点", not bad, f"{len(bad)} 处")
        for ln, txt in bad:
            print(f"      ❌ 行 {ln}: {txt}")
        all_bad += [(f, ln, txt) for ln, txt in bad]
        print()

    # JS 语法编译检查（node 可用时）
    print("── 内联 JS 语法 ──")
    node = None
    for cand in ("node", "node.exe"):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                node = cand
                break
        except Exception:
            continue
    if not node:
        print("   跳过（无 node）")
    else:
        for f in targets:
            path = os.path.join(WEB, f)
            js = (
                "const fs=require('fs');"
                f"const html=fs.readFileSync({path!r},'utf8');"
                "const re=/<script(?![^>]*\\bsrc=)[^>]*>([\\s\\S]*?)<\\/script>/g;"
                "let m,i=0,bad=0;"
                "while((m=re.exec(html))){i++;try{new Function(m[1])}catch(e){bad++;console.log('  #'+i+' '+e.message)}}"
                "console.log(i+' 块，失败 '+bad);process.exit(bad?1:0)"
            )
            r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
            out = (r.stdout or "").strip().replace("\n", " ")
            check(f"{f} JS 语法可编译", ok, out)

    # 汇总
    print("\n" + "=" * 70)
    total = len(RESULTS)
    passed = sum(1 for _, ok_, _ in RESULTS if ok_)
    print(f"共 {total} 项，通过 {passed} 项，失败 {total - passed} 项")
    if all_bad:
        print("\n未转义拼接点（必须处理）：")
        for f, ln, txt in all_bad:
            print(f"  {f}:{ln}  {txt}")
        print("\n处理方式：给数据包 esc()；或紧邻上一行写注释说明来源为数字/规范化时间/服务端生成。")
        print("结果: FAIL")
        return 1
    print("结果: PASS —— 所有拼接点已转义或有明确安全理由")
    return 0


if __name__ == "__main__":
    sys.exit(main())
