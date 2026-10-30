"""初始化本地数据库：建表 + 写入示例数据。

用法：
    .venv/Scripts/python.exe seed.py          # 仅当库为空时写入
    .venv/Scripts/python.exe seed.py --force  # 清空后重建

账号：注册体系已移除，账号由 app.init_db() → bootstrap_admin() 自动创建，
默认用户名 admin（可用 MEMO_ADMIN_USER 覆盖），**不设默认口令** ——
首次打开网页会强制进入「设置管理员密码」。
本脚本只负责灌示例数据（日程/标签/团队），不再创建账号。
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "memo.db")
DT = "%Y-%m-%d %H:%M"


def fmt(d):
    return d.strftime(DT)


def main(force=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    import app as memo_app  # 复用 SCHEMA
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(memo_app.SCHEMA)

    has_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if has_user and not force:
        print(f"数据库已有 {has_user} 个用户，跳过（需要重建请加 --force）")
        conn.close()
        return
    if force:
        for tb in ("event_tag", "subtasks", "events", "tags", "report_replies", "report_sends",
                   "reports", "shares", "invites", "memberships", "teams", "points_log",
                   "feedback", "member_applies", "notice_state", "visits", "user_setting",
                   "api_tokens", "idempotency", "notices", "users"):
            conn.execute(f"DELETE FROM {tb}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()

    now = datetime.now()
    # 账号由 bootstrap_admin 统一创建（默认 admin、无口令、待首次设置）。
    # seed 不再造第二个账号：注册体系已移除，多账号只应通过实际的团队邀请流程产生。
    conn.commit()
    created, admin_user = memo_app.bootstrap_admin(conn)
    if not created:
        admin_user = conn.execute(
            "SELECT username FROM users ORDER BY id LIMIT 1").fetchone()[0]
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (admin_user,)).fetchone()[0]
    if created:
        conn.execute("UPDATE user_setting SET show_huangli = 0 WHERE user_id = ?", (uid,))
        conn.execute("UPDATE users SET member_expire = ?, points = 300 WHERE id = ?",
                     (fmt(now + timedelta(days=27)), uid))
        conn.execute("INSERT INTO points_log(user_id, delta, kind, kind_text, remark, balance, time) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (uid, 300, "plan", "开通套餐",
                      "示例数据：月卡会员 30 天 + 300 积分", 300, fmt(now)))

    tags = {}
    for i, (name, color) in enumerate([("项目A", "#7F77DD"), ("运维", "#4f46e5"),
                                      ("汇报", "#06b6d4"), ("学习", "#10b981")]):
        cur = conn.execute("INSERT INTO tags(user_id, name, color) VALUES (?,?,?)", (uid, name, color))
        tags[name] = cur.lastrowid

    def add_event(user, title, content, start_off, end_off, remark, level, status, tag_names, subs, mins=0, repeat=""):
        s = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=start_off)
        if start_off == 0:
            s = now + timedelta(hours=2)
        e = s.replace(hour=18, minute=0) + timedelta(days=end_off - start_off) if end_off else s + timedelta(hours=6)
        cur = conn.execute(
            "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
            " repeat_rule, actual_minutes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (user, title, content, fmt(s), fmt(e), remark, level, status, repeat, mins, fmt(now), fmt(now)),
        )
        eid = cur.lastrowid
        for tn in tag_names:
            tid = tags.get(tn) if user == uid else None
            if tid:
                conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)", (eid, tid))
        for i, t in enumerate(subs):
            conn.execute("INSERT INTO subtasks(event_id, title, done, sort) VALUES (?,?,?,?)",
                         (eid, t, 1 if i == 0 else 0, i))
        return eid

    add_event(uid, "整理中台巡检报告", "汇总 ITIS/EASS/EBSS 三子系统当日异常",
              -3, -1, "结论先行，附证据截图", "high", "done", ["项目A", "汇报"],
              ["拉取 InfluxDB 数据", "运行 LLM 分析", "生成 HTML 报告"], mins=95)
    add_event(uid, "编写 AIOps 告警降噪方案", "梳理 dedup/silence/inhibit 三级规则",
              -1, 2, "", "high", "doing", ["运维"],
              ["现状调研", "规则设计", "灰度验证"], mins=40)
    add_event(uid, "每日站会", "同步当天排期与阻塞项", 0, 0, "", "middle", "doing", ["运维"], [], repeat="day")
    add_event(uid, "整理 Zabbix 39 节点采集清单", "核对 ssh_exporter 覆盖情况",
              1, 3, "缺口主机补纳管", "middle", "doing", ["项目A"],
              ["导出 hostid 列表", "比对 Ansible inventory"], mins=0)
    add_event(uid, "沉淀监控平台操作手册", "录制 VictoriaMetrics 常用查询",
              4, 6, "", "low", "doing", ["学习"], ["整理查询语法", "写成 FAQ"])

    tid = conn.execute("INSERT INTO teams(name, owner_id, remark, created_at) VALUES (?,?,?,?)",
                       ("运维小组", uid, "稳定性保障中心值班组", fmt(now))).lastrowid
    conn.execute("INSERT INTO memberships(team_id, user_id, role, status, join_time) VALUES (?,?,?,?,?)",
                 (tid, uid, "owner", "joined", fmt(now)))

    now_s = fmt(now)
    conn.executemany("INSERT INTO notices(title, content, kind, top, publish_time) VALUES (?,?,?,?,?)", [
        ("欢迎使用日历备忘录（本地版）",
         "基础功能永久免费：日程、日历、三色优先级、标签、子任务、归档与耗时统计全部可用。", "info", 1, now_s),
        ("AI 日报 / 周报说明",
         "本地版不依赖外部大模型服务，日报周报由内置模板汇总你的日程记录生成，可直接编辑后发送给组长。", "success", 0, now_s),
        ("会员功能演示提示",
         "本地版会员开通为演示模式：选择套餐提交后立即生效，不经过人工审核，也不产生真实扣费。", "warn", 0, now_s),
    ])
    conn.commit()
    conn.close()
    print(f"初始化完成：示例数据已写入，账号 {admin_user}")
    print("该账号尚未设置密码 —— 首次打开网页会强制进入「设置管理员密码」")
    print(f"数据库：{DB_PATH}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
