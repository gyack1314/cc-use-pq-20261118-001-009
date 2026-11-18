# 植入设备使用证据核算服务

面向研究数据专员的本地数据分析服务：登记匿名对象的观察期，导入有版本的使用会话，核算有效使用与观察分母，冻结可解释结果。所有演练数据均为本地合成数据。本服务不作医学诊断、不控制设备，也不证明记录缺口代表未使用。

Python 3.11，FastAPI 0.115.6。依赖版本见 requirements.txt。开发环境可用 `python -m pip install -r requirements.txt` 安装。启动：`python -m uvicorn evidence.api:app --host 127.0.0.1 --port 8000`。数据库由 EVIDENCE_DB 指定，默认 data/evidence.sqlite3；OpenAPI 文档位于 /docs。服务面向受信任本地环境，没有互联网身份认证和多租户隔离。

运行测试：`python -m unittest discover -s tests -v`；语法检查：`python -m compileall -q evidence`；运行合成演练：`python -m evidence.demo`。

## 数据契约

时间为带时区的整秒，统一按UTC解释。所有区间为左闭右开。subject_id、center、source、session_id 为1至64位ASCII标识符。对象观察期从植入时间起，结束可为空；修改必须提供当前 expected_version，新建填0。资料更正影响当前分析，历史资料版本保留。

会话唯一键为 source 与 session_id，revision 为递增正整数，可以跳号。相同修订相同内容可重放；同号异文、未知旧修订、跨对象改归属被拒绝。withdrawn 的新修订撤回当前会话，不能删除已发布证据。单批最多500条，不允许重复会话键，任何一条冲突会回滚整批。request_id 重放须保持整个有序批次相同。一次有实质变化的批次只推进一次 epoch；单纯重放不推进。

mode 为 control、training、calibration。质量区间须完全位于所属会话，重叠质量区间只扣一次；每条会话先扣除自身质量排除，再合并同一对象的有效时段。一个来源的低质量不会否决另一个来源的独立有效证据。同一对象重叠只计一次，不同对象时长相加。模式明细各自取并集，模式之间可能重叠，不能相加当作总使用时长。

查询最长366天，默认只纳入control。观察秒数是对象观察期与查询窗口的交集，不依赖是否收到会话。跨UTC日切分后，使用达到 min(active_day_seconds, 当天观察秒数) 算活跃日，默认阈值60秒。无分母的率值为null，零使用记录不等于证实零使用。总体和中心按实际观察秒数加权，另提供对象使用率中位数。

## 常用业务入口

PUT /subjects/{subject_id} 登记或更正，GET /subjects/{subject_id}/history 查询历史。
POST /sessions/batches 原子导入；GET /sessions/{source}/{session_id} 回查全部修订。
POST /analysis 返回总体、中心、对象、UTC日与会话决定；POST /analysis/subjects/{subject_id} 查询单人解释。
POST /analysis/inspection 查数据解释限制；POST /analysis/review 生成可选发布复核摘要。提供 review_digest 时，发布须准确确认 requires_acknowledgement 内的全部项目，且证据不能在复核后变化。
POST /releases 使用 expected_epoch 防止分析后发生更正仍签发旧口径；request_id 只对应一份发布请求。发布不推进数据epoch。GET /releases/{id} 返回不可变证据；/verify 核对摘要；/rebuild 使用原始历史修订重算；/current-difference 解释当前与冻结结果差异；/bundle.zip 导出确定性证据包和SHA256清单。
POST /analysis/periods 接受 before、after 两份查询，要求前期先结束、相同中心/模式/日阈值；返回整体、中心与配对对象的描述差异，分开列新增/退出观察人群，不解释为疗效或因果。
GET /audit 按游标查询数据更正与发布事件；GET /integrity 检查SQLite完整性。

## CSV交换

POST /tables/preview 与 /tables/import 接受 request_id、sessions_csv、可空 quality_csv。预览只检查当时状态，正式导入重新核验。会话表表头：source,session_id,revision,subject_id,start,end,mode,withdrawn,note；质量表表头：source,session_id,revision,start,end,reason。withdrawn 用 true 或 false。表头可以换顺序但不可增减列或重复。每表不超过4MB，会话最多500行，质量最多10000行。质量行必须对应本次会话修订。GET /tables/export/{source} 按会话键游标导出同样两张表，可原样重放；每页最多500条。

数据库采用WAL，每次分析在一致读事务内完成，批次与发布使用写事务互斥；对象修改采用版本比较，发布采用epoch比较。对数据库文件的外部手动编辑不受应用契约保护。请在停写后或使用SQLite备份机制保全数据库，不能只复制活跃WAL模式的主文件。
