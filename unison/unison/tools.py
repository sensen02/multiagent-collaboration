"""The same capability registry is given to root and descendant agents."""

def obj(properties=None, required=None):
    return {'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}
S = {'type':'string'}
A = {'type':'array','items':S}
I = {'type':'integer'}
B = {'type':'boolean'}
J = {'type':'object','additionalProperties':True}

# 所有 Agent 拿到**同一套**工具：不按角色预制档位。运行时的职责是提供能力，
# "这个角色用不用得上某个工具"由模型自己判断，不由运行时替它裁剪。
TOOLS = []


def tool(name, description, properties=None, required=None):
    return {'type':'function','function':{'name':name,'description':description,'parameters':obj(properties,required)}}


def schemas_for(groups=('all',)):
    """返回全量工具 schema。保留签名，便于既有调用点无需改动。"""
    return list(TOOLS)

TOOLS = [
 tool('models_list','查看可用模型及能力；自主选择，没有自动路由。'),
 tool('models_credentials','读取模型调用凭据（含明文 API Key）、地址与协议，用于在自己的代码里直接调用该接口。',
      {'model_ids':A}),
 tool('tasks_list','查看同一运行的任务、当前版本和状态。'),
 tool('tasks_submit','创建子任务，可继续递归委派；在独立工作副本中运行。**子模型要按任务挑**：不指定会默认继承你自己的模型，短任务请显式点便宜档，并用 cost_note 说明理由（会记进任务记录）。',
      {'goal':S,'model_id':S,'dependencies':A,'priority':I,'reuse_key':S,
       'cost_note':{'type':'string','description':'为什么给这个子任务选这个模型/档位。'}},['goal']),
 tool('tasks_yield','保存现场并释放执行槽，订阅事件；必须为本轮最后一个工具。`task_ids`=这些任务结束**或给你发消息**；`topics`=消息主题，**一旦声明就成为严格过滤器**（漏掉的 topic 不再唤醒你）。禁止订阅自身 id，也不要在子任务里等父级。**时限由你自己声明**：不写 timeout_seconds/max_wait_seconds 就是"只被订阅的事件唤醒"，不会自动到期；不要靠超时轮询。会话类任务不会结束，别等它们结束。',
      {'task_ids':A,'topics':A,'mode':{'type':'string','enum':['any','all']},'after_cursor':I,
       'timeout_seconds':{'type':'number'},'max_wait_seconds':{'type':'number'},'note':S}),
 tool('agents_send','给另一个任务发短消息，**可直接发给任意同版本任务（含兄弟），不必经父级中转**（每多一次中转就多一次模型调用与上下文）。inbox 入队，wake 合批唤醒；进度不要 wake。三种用法：①自己提问带 request_id；②答复带 in_reply_to=对方 request_id；③替别人转达带 relay_of=该 request_id（保留原 ID，答复才能绑回最初的问题）。不带 in_reply_to 的裸答复在多问并发时不会被采纳。',
      {'task_id':S,'summary':S,'topic':S,'delivery':{'type':'string','enum':['inbox','wake']},'kind':S,'artifact_refs':A,
       'request_id':S,'in_reply_to':S,'relay_of':S},['task_id','summary']),
 tool('tasks_close','收束某任务前先排空它在途请求：mode=drain 等它答完（或到时限），mode=forfeit 显式作废并通知对方不必再答。不要在对方还有未答问题时强行让它汇报，那样会留下无人认领的答复。',
      {'source_task':S,'mode':{'type':'string','enum':['drain','forfeit']},'request_ids':A,'timeout_seconds':{'type':'number'},'reason':S},['source_task']),
 tool('tasks_cancel','取消任务及子树；历史产物保留。',{'task_id':S},['task_id']),
 tool('human_ask','向人类提出问题。**整个运行会因此暂停**：所有 Agent 做完当前这一轮就停手，人类回答后自动继续，回答作为消息回到你手里。所以提问前先想清楚——这一问会让全队一起等；答案不依赖人类的问题（读文件、跑测试、问同伴）不要用它。必须为本轮最后一个工具。',{'question':S,'options':A},['question']),
 tool('workspace_list','列出文件或目录。所有模型均可直接操作代码。',{'path':S}),
 tool('workspace_read','按行读取文件，默认最多 200 行，长文件按需读取。',{'path':S,'start':I,'limit':I},['path']),
 tool('workspace_image_info','读取 PNG/JPEG/WebP/GIF 文件头，返回格式、存储尺寸（不应用 EXIF 旋转）、字节数与 SHA-256；不解码像素，不判断图片内容或完整可解码性。',{'path':S},['path']),
 tool('workspace_write','写入完整文件内容，自动保留差异。',{'path':S,'content':S},['path','content']),
 tool('workspace_edit','精确替换文件片段；匹配必须唯一。',{'path':S,'old':S,'new':S},['path','old','new']),
 tool('workspace_shell','执行 shell 命令。输出原文保存，可检索；可运行代码和测试。环境变量 UNISON_MODEL_BASE_URL / UNISON_MODEL_API_KEY / UNISON_MODEL_ID / UNISON_MODEL_API 已注入当前模型调用信息。',{'command':S,'timeout_seconds':I},['command']),
 tool('workspace_diff','查看当前任务相对基线的实际文件差异。'),
 tool('workspace_integrate','把子任务结果三方合并到本任务工作区；冲突返回记录，由 AI 自行选择如何解决。',{'source_task':S},['source_task']),
 tool('workspace_archive','归档当前运行的日志、差异与内容对象，不修改用户文件。'),
 tool('workspace_restore','从归档恢复到新的工作区，不覆盖现有目录。',{'archive_path':S,'task_id':S,'version':{'type':'string','enum':['before','after']}},['archive_path']),
 tool('artifacts_read','读取工具原文或内容对象，按字符分页。',{'ref':S,'start':I,'limit':I},['ref']),
 tool('knowledge_search','检索共享知识（按关键词与相关度）；来源过时的记录明确标记，不可当作当前事实。注意它是**检索**：结果按相关度排序并默认截断，两个人搜同一个词也可能拿到不同切片——要"所有人都看到同一份"，请用 knowledge_read(scope="run") 按序号读取。',
      {'query':S,'scope':{'type':'string','enum':['project','run'],'description':'限定范围（可选）：project 项目知识；run 本次运行内的共享信息。留空为全部。'}},['query']),
 tool('knowledge_read','读取共享信息。两种用法：①按 id 读一条（含出处与是否失效）；②不传 id、用 scope+from_seq 按**序号顺序枚举**——这是"所有人看到同一份"的唯一可靠读法，不会因相关度排序或截断而出现差异。',
      {'id':S,'scope':{'type':'string','enum':['project','run']},'from_seq':I,
       'limit':{'type':'integer','description':'0 或省略表示不截断'}}),
 tool('broadcast','发布一条**共享信息**，让需要的人自己读同一份——而不是把内容抄进别人的任务描述（抄一次就产生一个分叉，各方看到的历史会不一致）。两种范围：scope="project"（默认）＝可复用的项目知识/结论，要提供来源文件；scope="run"＝本次运行内的公告、状态或发言（例如一局游戏里"我这一轮的描述"），它不进项目知识索引、也不跨运行。notify=[task_id…] 会把这条**主动推送**给这些任务（收件箱里直接可见，不必去搜）——"让所有人看到我的发言"就该这么做。',
      {'title':S,'content':S,'sources':A,'dependencies':A,'goal_specific':B,
       'scope':{'type':'string','enum':['project','run'],'description':'project 项目知识（默认）；run 本次运行内共享'},
       'notify':{'type':'array','items':S,'description':'要推送到的任务 id 列表（可选）'}},['content']),
 tool('skill','加载某个可用技能的完整指令。当任务点名某个技能、或明显匹配某条技能描述时，先用本工具按精确名称加载，再开始动作。技能正文与 assets/scripts/references 都是普通文件，可用 workspace_* 读取或执行。声明了 invocation 契约的技能可直接通过端口批量调用（见 skills_list 的 invocable/batch）。',
      {'name':S,'batch':{'type':'array','items':J}}),
 tool('skills_list','列出当前可用技能及其调用契约：是否可用端口调用（invocable）、是否接受批量（batch）、最大批量与默认超时。需要一次生成多份产物时按这里的说明提交批量请求，而不是在 shell 里串行重复执行。'),
 tool('usage_report','查看本运行的模型用量（调用数、tokens、重试次数）、当前在途/排队情况与各模型的额度桶。判断"还能不能再委派一轮"时用它，而不是猜。'),
 tool('context_compact','立即压缩工作上下文：本轮工具批次结束后生效，无需批准；原始日志保留。'),
 tool('goals_activate_revision','根据用户已经提出的变更切换新目标；停止旧计划，历史成果可再采纳。',{'goal':S,'change_message_id':S},['goal','change_message_id']),
 tool('tasks_complete','提交结果。文件原因按路径填写，运行时对账遗漏。完成不等于测试通过。自己发出、还没人回答的请求在提交时会被**自动作废**并通知对方"不必再答"（写 RequestsAutoForfeited 记账），不会卡住你，也不会把对方留在等一个永远不会来的答复上；想自己说明原因就用 forfeit_requests+forfeit_reason，那会优先按你给的原因处理。',
      {'summary':S,'file_reasons':J,'evidence':A,'unresolved':A,'verified':B,
       'verification_status':{'type':'string','enum':['verified','failed','not_verified','unknown']},
       'impact':S,'unknowns':A,'forfeit_requests':A,'forfeit_reason':S},['summary']),
]

SYSTEM = '''你是 Unison 本地协作系统中的一个智能体。主与子使用相同工具和运行循环。
根据用户目标自主选择直接读写代码、执行测试、委派模型、与其他任务协作或询问人类。
不要为了简单任务必经固定流程。所有模型受信任且具有同样工具能力。
子任务默认在独立工作区执行；需要将成果带回父工作区时调用 workspace_integrate，然后验证。
日志由运行时自动记录，不必主动提交 Git 以保存历史。
消息是短摘要加产物引用：普通进度使用 inbox，真正阻塞或需要介入时才 wake。不用发消息汇报每个工具调用。
任务之间可以直接互相发消息（含兄弟任务），不必经过父级中转；经第三方转发会让每次往返多耗一次模型调用与上下文，能直连就直连。
并发提多个问题时给每个问题带 request_id，答复时带 in_reply_to：只回一个“是”而不带关联，在该目标有多个未答问题时不会被采纳。
收束别人之前先用 tasks_close 排空它在途请求；自己收束时若还有未答请求，提交即自动作废并通知对方（也可用 forfeit_requests 自己说明原因）。
等待使用 tasks_yield：`task_ids` 是"这些任务结束或给你发消息"，`topics` 是消息主题；声明了 topics 就成了严格过滤器，只订阅话题时漏掉真正在等的主题会让你永远醒不过来（要按对方发送时用的 topic 原样订阅）。**时限由你自己声明**：不写 timeout_seconds/max_wait_seconds 就意味着"只被订阅的事件唤醒"，运行时不会替你补一个兜底时限，也不会自动顺延——想被叫醒就声明时限，或订阅会真正发生的事件；不要靠超时轮询。**游戏/会话类任务不会自己结束**：子任务在会话中通常停在等待上，不要"等它们结束"——订阅它们的消息或用时限。若你订阅的任务全部停在等待、且整个会话没有任务在运行，那说明继续等下去不会有新事件，请自己收束、提问或换等待条件。
**把钱花在刀刃上（每次委派前都要过一遍）**：模型调用与本地算力花的是用户真实的钱和时间，不是免费的。所以
①**子模型要按任务挑，默认"和我一样"是最省事的错**：短任务（写用例、出题、判分、单轮问答、格式转换）用便宜档，只有确实需要长链推理、复杂重构或高可靠判断时才上重档——重档多花的不只是 token，还有排队时间。
`models_list` 会给每个模型的 tier / health / capacity，先看它再决定；**tier 只是相对档位的标签，不是价格表**（系统里没有单价数据），所以"哪个更贵"要按档位与调用规模估，别当成精确账。②**能一次做完就不要分两次**：每次调用都要重新装载系统提示与工具 schema（实测固定约 21.0 KB：提示 7.2 KB + 工具 13.9 KB），空转一轮、重复汇报、为同一件事再开一个子任务都要重复付这笔钱。③**能自己做就别委派**：改一行、跑一条命令这种活直接做，委派的开销大于收益。④**本地算力与模型调用是同一笔预算的两半**——显卡是整个 run 共享的稀缺资源，多任务同时往卡上塞会互相排队甚至爆显存。要用本地推理前先看 `usage_report` 的 `local_compute`（显存余量、占用率、系统负载），余量不够就少开并发或改走外部服务。本地与外部**两条路都正当**：本地适合"要求离线/不外传数据、要可复现、外部给不了或不可靠"；外部适合"本地算力被占满或排队太久、本地缺对应模型、或外部质量明显更好"。跑模型/生图生视频前先看技能目录里的 gpu 技能（`python3 <base>/scripts/probe.py` 报告本机现在能用什么），选定后在交付说明里写清走了哪条、为什么——**尤其是切换路线时不要悄悄换掉**。⑤**实在分不清就在交付说明里写清"为什么选它"**（`tasks_submit` 的 `cost_note`），让人类能纠正你的判断，而不是悄悄烧钱。
**想少花钱，先看清楚哪些等待是白等的**：会话/游戏类任务不会自己结束，子任务在会话里通常停在等待上，所以"等它们结束"多半永远等不到。要它们有动静就叫你，就在 `tasks_yield` 里写 `topics=[...]` **并且把对方发送时用的 topic 原样写进去**（不写时限也不写 topics 就只能等事件自己来；`topics` 写错等于给自己装了个耳塞，而空 `topics` 的等待会直接收到"此等待不可能被满足"的警告）。对方用 `delivery=wake` 发的消息也能立刻叫醒你。
你可以读取共享知识以避免重复总结 README；检查来源和 stale 标记。
共享信息只有两种正确用法：①可复用结论 → broadcast(scope="project", sources=[...])；②本次运行内的公告/状态/发言 → broadcast(scope="run", notify=[要看到的人...])，让它主动推送。
**绝对不要把别人的发言或共享信息复制进另一个任务的描述（goal）**：复制一次就产生一个分叉，各方看到的历史不一致、也不公平。要"所有人都看到同一份"，就用 knowledge_read(scope="run", from_seq=0) 按序号读——它不过滤、不截断、顺序确定，任何人读到的都一样。
系统提示里会附一份技能目录（只有名称与摘要）：任务匹配某个技能时，先用 skill 工具加载它的完整指令再动手。
带 invocation 契约的技能可以走端口批量调用：用 workspace_shell 里的 curl 调 POST /api/skills/call（提交）与 GET /api/skill/jobs/<id>（轮询），认证用 Authorization: Bearer $UNISON_API_TOKEN。多份同类产物优先一次批量提交，不要在 shell 里串行重复执行。
长工具结果通过 artifacts_read 分页读取；context_compact 只在确有需要时调用，调用后本轮结束即生效。
模型凭据是可用能力：可用 models_credentials 读取明文 Key，或在 shell 里直接用 UNISON_MODEL_* 环境变量调用模型接口。凭据只用于调用，不要写进文件、报告或知识。
核心需求有版本，旧任务不再执行，但旧产物可在当前版本重新检查、集成。以当前目标为准。
任务结束请调用 tasks_complete，并对实际影响的每个文件说明原因，列出真实验证证据和未解决项。
**纯文本不算交付，也不会送达任何人**：只写一句话既不发给对方，也不算结项。要答复别人用 agents_send，要等消息用 tasks_yield，要结束用 tasks_complete。若你这一轮只输出纯文本而收件箱里还有未读消息、或你自己的请求还没人回答、或有子任务未结束，运行时会把这一轮退回队列并提醒你（退回次数不限，也不会因此判负）。持续服务型角色（出题人、主持人、答疑者）尤其注意：每轮都要用工具表态。
并发时消息可能在你推理期间到达（收件箱在步骤开始时清空）：这一轮看不到它很正常，下一轮会被退回给你处理，不要以为会丢。
只有真实验证支持时 verified 才能为 true。你给出的证据会与真实执行记录绑定；运行时只记录事实（命令原文、退出码、是否超时），**不替你判断证据够不够**——那由你和人来判断。
human_ask、tasks_yield、tasks_complete 应作为当前一轮最后一个工具调用。
**human_ask 会让整个运行停下来**：不只是你，所有 Agent 做完手里这一轮都停手，直到人类回答。当前这一轮不会被掐断（你写的文件照常落盘），但不会有人再开始新一轮。因此它是"只有人类能回答"时才用的工具；答案自己能查到的，别拿去占用人类。
'''
