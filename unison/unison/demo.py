"""Explicitly labeled deterministic demo. It exercises real runtime tools, no LLM claim."""
import asyncio
import json
from .store import uid, dumps

async def demo_call(config,messages,tools):
    await asyncio.sleep(.25)
    if not tools:
        return {'role':'assistant','content':dumps({'decisions':['离线演示检查点'],'pending':[],'evidence_refs':[]})},{}
    # 任务简报是第一条 user 消息；不能按下标取，因为系统消息条数会随版本变化
    # （基础提示 + 技能目录），下标会漂移。
    brief=next((m.get('content') or '' for m in messages if m.get('role')=='user'), '')
    try:
        goal=json.loads(brief).get('goal','')
    except (ValueError, AttributeError):
        goal=brief
    actions=[]; results=[]
    for m in messages:
        if m['role']=='assistant':
            for c in m.get('tool_calls',[]): actions.append(c['function']['name'])
        elif m['role']=='tool':
            try: results.append(json.loads(m['content']))
            except ValueError: pass
    def call(name,args):
        return {'role':'assistant','content':None,'tool_calls':[{'id':uid('call_'),'type':'function','function':{'name':name,'arguments':dumps(args)}}]},{}
    n=len(actions)
    if goal.startswith('离线演示'):
        children=[x['task_id'] for x in results if isinstance(x,dict) and 'task_id' in x and 'workspace' in x]
        if n==0: return call('tasks_submit',{'goal':'演示文档：创建说明，并请子任务检查文本'})
        if n==1: return call('tasks_submit',{'goal':'演示问答：询问用户希望采用的名称，然后保存'})
        if n==2: return call('tasks_yield',{'task_ids':children,'mode':'all','after_cursor':0,'note':'等待两个分支，可通过插话唤醒'})
        integrated=[x for x in results if isinstance(x,dict) and x.get('integrated')]
        if len(integrated)<len(children): return call('workspace_integrate',{'source_task':children[len(integrated)]})
        return call('tasks_complete',{'summary':'离线机制演示完成：子任务递归、人工问答、文件集成和知识发布已执行。此运行使用确定性脚本，并非真实模型推理。',
                                      'file_reasons':{'collaboration.md':'记录递归协作演示','choice.txt':'保存用户回答'},'verified':False})
    if goal.startswith('演示文档'):
        if n==0: return call('tasks_submit',{'goal':'演示检查：确认协作说明的表达'})
        if n==1:
            child=next(x['task_id'] for x in results if isinstance(x,dict) and 'task_id' in x)
            return call('tasks_yield',{'task_ids':[child],'after_cursor':0})
        if n==2: return call('workspace_write',{'path':'collaboration.md','content':'# 协作记录\n\n主任务与子任务使用同一种运行循环。\n等待时释放执行槽，相关消息可唤醒。\n'})
        if n==3: return call('broadcast',{'title':'协作说明','content':'collaboration.md 记录递归协作和事件等待。','sources':['collaboration.md']})
        return call('tasks_complete',{'summary':'说明文件已生成，子任务检查完成。','file_reasons':{'collaboration.md':'说明递归协作行为'}})
    if goal.startswith('演示问答'):
        if n==0: return call('human_ask',{'question':'这份演示使用哪个项目名称？','options':['Unison','协同工作室']})
        answer='Unison'
        for m in messages:
            if m['role']=='user' and '用户回答：' in m.get('content',''):
                try:
                    batch=json.loads(m['content'].split('收件箱批次：',1)[1])
                    for item in batch:
                        if item['summary'].startswith('用户回答：'): answer=item['summary'].split('：',1)[1]
                except ValueError: pass
        if n==1: return call('workspace_write',{'path':'choice.txt','content':answer+'\n'})
        return call('tasks_complete',{'summary':'用户选择已保存。','file_reasons':{'choice.txt':'记录用户指定名称'}})
    return call('tasks_complete',{'summary':'表达检查完成（离线演示）。'})
