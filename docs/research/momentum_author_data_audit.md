# Momentum 作者公开数据审计

本轮实际读取并校验了 [Harvard Dataverse V2](https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/R1UI1J&version=2.0) 的 `IntnlData.mat` 与 `USData.mat`。结果支持建立来源审计、原行切片和 MOM / DGW 时间对齐诊断，尚不支持宣称真实市场回测或论文收益复现。

作者论文说明公开数据经过扰动和随机删减。公开复制包的许可不能替代商业原始数据库的授权。DGW 是作者预计算字段，包中没有用于重建该字段的每日收益链路。匿名资产行不是证券代码。

## 固定来源与实际结构

| 文件 | 字节数 | 官方 MD5 | 本地 SHA256 |
|---|---:|---|---|
| IntnlData.mat | 358892265 | 87d9424ee3b5b2fce4a4fb3646cf6df6 | 6fe6fc27a85ec0f305d297b95d05039cb7bd2207ce012625df1e44bc272549ed |
| USData.mat | 93635256 | a84f4ab4be1b9a26f4822b048abad9fe | 0b3f60867708c707816caa9ef856d5579c9c732af24acc734bb47d387ee4ab1d |

- Intnl：HDF 中 Return / DGW / MV 均为 **384 个月 × 59367 个原资产行**；月份 1989-01 至 2020-12，连续且不重复。MATLAB 脚本所见的 N×T 与 HDF 读取的 T×N 需要显式区分。
- US：对应形状 **1140 × 25437**；从原 `ymd` 提取的月份为 1926-01 至 2020-12，连续且不重复。
- US 的 308 个原日期不是日历月末，例如 1926-01-30。工具保留原日期并按其月份对齐，不填补日历月末数据，也不把这些日期判成缺月。Intnl 没有日日期，保存 null。
- Intnl 的 49 个 CountryCodes 唯一；NumAll 为正整数且总和等于 59367。以原行的累计区间映射国家，不按非缺失值重新编号。US 不提供该映射，country 保存 null。
- **修正早期研究卡的结论**：ATGrowth、BM、COGStoAT、GPtoAT 实际均在 Intnl 主文件内，形状 384×59367。缺失的是脚本引用的外部 `Intnl_WCMonthly.mat` 路径，不能再概括为这些字段缺失；同时尚未证明内嵌同名字段与该外部文件内容完全等价。

## 全源质量统计

下表来自对固定摘要文件的实际分块扫描；非有限值不会转换成零。

| 源 / 字段 | 有限值 | NaN | 有限最小值 | 有限最大值 | 零值 |
|---|---:|---:|---:|---:|---:|
| Intnl Return | 6167046 | 16629882 | -0.9999047702095364 | 1.9999999399999941 | 42959 |
| Intnl DGW | 5842002 | 16954926 | -1 | 0.9916666746139526 | 176092 |
| Intnl MV | 6188687 | 16608241 | 0 | 53889869134.70701 | 5416 |
| US Return | 1782585 | 27215595 | -0.9936000108718872 | 24 | 81851 |
| US DGW | 1639553 | 27358627 | -0.40086206793785095 | 0.27826088666915894 | 49976 |
| US MV | 1795846 | 27202334 | 0 | 2024064640 | 59 |

两个源的三个核心字段均没有发现正负无穷。Return 没有小于 -1 的有限值；DGW 没有超出 [-1,1]；MV 没有负值。Intnl / US 分别有 15250 / 4303 个 Return 大于 1：高收益本身不是格式错误，读取工具保留原值，不进行隐式裁剪。MV=0 同样保留原值，但在形成可用性诊断中标为不可用。

Intnl Return 的 1989 年 12 个月全空，DGW 的 1989-01 至 1990-10 共 22 个月全空，MV 的 1989-01 至 1989-11 共 11 个月全空。US DGW 前 10 个月全空。这些是字段覆盖不足，不是月份轴断裂。

## 时间对齐与公开删减的影响

目标收益月 H 的 MOM 用 H−12 至 H−2，共 11 个自然月；H−1 是跳过月，也是本次读取 DGW / MV 的形成月份；收益标签只读取 H。提取输入包含连续 H−12 至 H 的 13 个月原 Return。不得以 H 的收益是否可用反向筛选形成样本。

本项目的 `formation_ready` 只检查诊断可用性：11 个月 Return 全部有限且不低于 -1、MOM 数值可表示、H−1 DGW 位于 [-1,1]、H−1 MV 为有限正数。它不代表满足作者 REIT、微盘、极值处理、国家中性化等全部资格规则。

在**全文件所有原资产行**上计算上述条件，结果如下。未挑选较完整资产，没有填补缺失。

| 目标月 | Intnl 形成可用 | 其中标签缺失 | US 形成可用 | 其中标签缺失 |
|---|---:|---:|---:|---:|
| 1993-03 | 1 | 0 | 2 | 2 |
| 2000-01 | 6 | 1 | 5 | 2 |
| 2015-01 | 4 | 0 | 2 | 2 |
| 2020-01 | 15 | 8 | 0 | 0 |

这不是有界切片造成的样本量不足：即使扫描整个包，完整窗口仍很少。默认取原行 1–128、目标月 1993-03 时，两个源的形成可用数都为零，标签可用数分别为 12 和 19。零可用行是正确且有价值的诊断产物，不能为了得到收益曲线改短窗口、填补或按照未来标签选行。

作者 Table8 的初始样本门槛为 450，且每个角落组合存在额外资格和标签筛选。其未来收益缺失后重归一权重的统计口径，与 v0.14 冻结权重后报告不可评估的项目口径不同。本轮不执行该表，不产生组合收益、成本、Sharpe 或经济效果结论。四个审计月份是固定的覆盖检查点，不是收益调参、训练 / 验证 / 最终测试划分。

## 可复现的工具边界

公共接口位于 `paper_alpha.author_archive`：

```python
audit(path)
extract_panel(path, target_month, row_offset=0, row_count=128)
```

两者都先核对固定源的完整字节数、官方 MD5 与 SHA256，再读取同一个已打开文件句柄；前后检查路径、inode、大小、mtime / ctime 与链接数。文件名限定两个固定源，不能靠给任意 MAT 改名导入。不会联网或执行原 MATLAB。

HDF 检查发生在任何 dataset 数据读取前：只接受平坦的数值 dataset 和内建 DEFLATE；拒绝软链接、外链、重复对象硬链接、外部存储、virtual dataset 和插件滤镜。计算分块沿资产轴进行，不一次加载整个面板；这也符合实际 HDF chunks 跨越时间轴的存储方式。

切片仅接受连续原行，最多 512 行；资产身份由源文件名和原始 1-based 行号构成。NaN / 正无穷 / 负无穷分别保存为 null 与对应状态，有限零值保留。输出再通过闭合的 normalized-panel 契约校验。

```sh
python -m paper_alpha.author_workflow audit \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --out artifacts/author-intnl-audit-example

python -m paper_alpha.author_workflow prepare \
  --source artifacts/research-momentum-data-01/IntnlData.mat \
  --target-month 1993-03 --row-offset 0 --row-count 128 \
  --out artifacts/author-intnl-panel-example

python -m paper_alpha.author_workflow verify \
  artifacts/author-intnl-panel-example \
  --source artifacts/research-momentum-data-01/IntnlData.mat
```

输出目录必须尚不存在。完整原 MAT 不上传 HTTP，也不进入源码发布包。工作台导入 JSON 只校验冻结面板、诊断与独立参考，不能仅凭 JSON 中声明的 SHA256 认证原始 MAT；需要上述带 `--source` 的本地核验才能核对原行。

## 验证与仍未解决的部分

`tests/test_author_archive.py` 使用明确标注的 tiny HDF 工程夹具，在测试范围内临时替换固定元数据；这些夹具不是作者数据。覆盖时间轴、字段形状和类型、原行及国家边界、未来标签独立性、非有限状态、摘要不符、文件变更、文件及 HDF 链接、virtual / external / 插件存储和分块读取。

该工具不能恢复作者删除的数据，不能推断缺失股票的退市收益，不能还原每日 ID，也不证明股票池是点时可得或市场收益可交易。本轮完成的是可核验的作者源接入及时间对齐诊断；继续做 Table8 方法移植和真实市场研究，需要单独冻结方法、补充适当数据，并保留这一来源限制。
