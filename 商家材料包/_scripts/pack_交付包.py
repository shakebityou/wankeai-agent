# -*- coding: utf-8 -*-
"""把商家材料包的「交付物」打成一个 zip。

范围（按用户选择）：README.md + data/（含商品图片素材库）+ docs/
不含：_scripts（生成/验收脚本）、_checks（office 检查输出）、_img_cache（爬取缓存）、.keep

用 Python zipfile 而非 Compress-Archive：对中文文件名可正确写 UTF-8 标志位，
Windows 资源管理器与 macOS/Linux 解压均不乱码。
"""
import os
import sys
import zipfile
from datetime import datetime

SRC = r"D:\learn\homework\agent\商家材料包"
OUT_DIR = r"D:\learn\homework\agent"
STAMP = "20260101"
OUT = os.path.join(OUT_DIR, "玩可爱商家材料包_{}.zip".format(STAMP))

TOP = "商家材料包"          # zip 内顶层目录名（与 README 目录树一致）
EXCLUDE_DIRS = {"_scripts", "_checks", "_img_cache", "_probes", "_raw_dialogues"}
EXCLUDE_FILES = {".keep", "Thumbs.db", "desktop.ini"}

DELIVERY_NOTE = """# 交付包说明

> **包名**：玩可爱商家材料包_20260101.zip
> **交付方**：玩可爱（WANKEAI）商家侧
> **接收方**：玩可爱智能客服 Agent 项目组
> **打包日期**：2026-01-01

---

## 一、本包内容

| 目录 / 文件 | 说明 |
|---|---|
| `README.md` | **总入口**，先看这个。含 S-01~S-22 交付对照表、完成度总览、目录结构、脱敏与安全说明、验收结果 |
| `data/` | 19 个结构化数据文件（CSV + XLSX 双份）+ `商品图片素材库/`（296 张图，5 类目 / 74 SKU 两级目录） |
| `docs/` | 11 份文档（售后政策、物流 SOP、营销规则、品牌 IP、接口说明书与权限清单、渠道资质、服务器环境、数据合规、预算成本、现有知识库资产） |

**未包含**（在商家本地保留，按需另行提供）：

| 未包含项 | 原因 | 如何获取 |
|---|---|---|
| `_scripts/` | 生成与验收脚本，属商家内部工具，非交付物 | 如需自行复跑验收，向商家 IT 部索取 |
| `_checks/` | office 文件结构检查输出（JSON） | 同上 |
| 接口 Token / Secret | 按脱敏要求不得写入文件 | 加密压缩包 + 单独渠道发密码 |
| 测试账号密码 | 同上 | 同上 |
| 对接人真实联系方式 | 同上 | 企业微信单独发送 |
| 平台 OAuth 授权 | 店铺主账号不外发 | 按渠道申请流程开通 |

---

## 二、数据质量

`README.md` 第 6 节列出的 **237 项验收全部 ALL PASS**，由商家侧 9 个脚本（`verify_01.py` ~ `verify_09.py`）自动校验，覆盖：字段完整性、条数下限、枚举取值、脱敏零残留、条款交叉引用可追溯、图片内容唯一性、全局约束（无外链跳转话术）等。

**本包内的 CSV 与 XLSX 内容一致**，可任选一种使用。

---

## 三、使用前必读

1. **S-02 商品图片**：296 张是**爬取的同类目相似参考图，不是本店真实商品照片**。仅可用于内部图搜链路打通与评测，**不得对外当作商品主图发布**。每张图的来源页面与授权情况记录在 `data/商品图片来源与署名.csv`。正式上线前须由商家用真实照片按同一命名规则替换（目录结构 `类目/SKU/文件名` 保持不变）。

2. **联系方式类字段**：`data/业务对接人信息表.csv`、`data/ERP供应商信息表.csv` 中的联系方式列统一标注「走安全渠道提供」，实际号码不在包内。

3. **跳转类能力本期不接入**：接口定义中保留了 `msg_type = GOODS/CARD` 与 `handoff_url` 字段，但本期**不实现**。对话流程遇到该场景直接绕过，客服回复一律纯文字引导，不生成、不发送任何外链或跳转卡片。详见 `docs/接口对接说明书.md` 第 0.8 节与 `README.md` 第 7.4 节。

4. **关键路径风险**：抖音小店渠道需**服务商认证（10 个工作日）**，是全项目关键路径，建议项目组第 1 周即启动。详见 `docs/渠道接入资质说明.md`。

---

## 四、建议阅读顺序

1. `README.md` —— 全局认识，10 分钟
2. `README.md` 第 1 节 —— S-01~S-22 交付对照表
3. `data/商品主数据表.xlsx` —— 摸清商品口径
4. `docs/接口对接说明书.md` —— 后端对接前必读
5. `docs/售后政策.md` + `docs/物流发货SOP.md` + `docs/营销活动规则.md` —— 知识库建设的口径依据

---

## 五、一句话结论

**索取清单要求的 S-01 ~ S-22 共 22 项静态资料已全部交付，可直接开工。**
唯一卡点是抖音小店服务商认证，建议立即启动。
"""


def should_skip(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if parts[-1] in EXCLUDE_FILES:
        return True
    if parts[-1].startswith("~$") or parts[-1].endswith(".tmp"):
        return True
    return False


def main():
    if not os.path.isdir(SRC):
        sys.exit("源目录不存在: " + SRC)

    files = []
    for root, dirs, fs in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in fs:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, SRC)
            if should_skip(rel):
                continue
            files.append((p, rel))
    files.sort(key=lambda x: x[1])

    total_raw = sum(os.path.getsize(p) for p, _ in files)
    print("待打包文件 {} 个，原始大小 {:.2f} MB".format(len(files), total_raw / 1048576))

    if os.path.exists(OUT):
        os.remove(OUT)

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        # 交付说明放最前面，便于解压后第一眼看到
        z.writestr("{}/交付包说明.md".format(TOP), DELIVERY_NOTE)
        for p, rel in files:
            arc = "{}/{}".format(TOP, rel.replace(os.sep, "/"))
            z.write(p, arc)

    size = os.path.getsize(OUT)
    print("已生成: {}".format(OUT))
    print("压缩后: {:.2f} MB（压缩率 {:.1f}%）".format(
        size / 1048576, 100.0 * size / total_raw))

    # 完整性校验
    with zipfile.ZipFile(OUT) as z:
        bad = z.testzip()
        names = z.namelist()
    print("完整性: {}".format("OK ✅" if bad is None else "损坏 ❌ " + str(bad)))
    print("包内条目: {}".format(len(names)))

    # 分目录统计
    stat = {}
    for n in names:
        if n.endswith("/"):
            continue
        seg = n.split("/")
        key = seg[1] if len(seg) > 1 else "(顶层)"
        if key == "商品图片素材库":
            key = "data/商品图片素材库"
        elif len(seg) > 2 and seg[1] == "data" and "." not in seg[2]:
            key = "data/商品图片素材库"
        stat[key] = stat.get(key, 0) + 1
    print("\n包内分布:")
    for k, v in sorted(stat.items()):
        print("    {:<24} {} 个".format(k, v))

    # 关键文件抽查
    print("\n关键文件抽查:")
    for must in ["README.md", "交付包说明.md",
                 "data/商品主数据表.xlsx", "data/FAQ知识库.csv",
                 "data/商品图片索引.csv", "data/商品图片来源与署名.csv",
                 "docs/售后政策.md", "docs/接口对接说明书.md"]:
        arc = "{}/{}".format(TOP, must)
        print("    {} {}".format("✅" if arc in names else "❌", must))
    imgs = [n for n in names if "/商品图片素材库/" in n and n.lower().endswith(".jpg")]
    print("    {} 商品图片 {} 张".format("✅" if len(imgs) == 296 else "❌", len(imgs)))


if __name__ == "__main__":
    main()
