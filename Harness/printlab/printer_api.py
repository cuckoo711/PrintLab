"""Choose which Bambuddy printer the workbench is bound to.

Same rules as the other browser controls: same origin, a session cookie and a CSRF header on every change.
Switching only rewrites ``printer_id`` in Runtime/config.json through ``workspace.configure_connection``.
It does not touch the device card, credentials, projects or any print task, and it never sends anything.

A switch is refused while a print task is active or its outcome is unknown, and when the chosen printer is not
one Bambuddy lists. Approvals and slices already carry a printer id; they are checked against the current
printer when they are used, so after a switch they ask for a new slice instead of being reused silently.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import bambuddy, core, onboarding, workspace
from .prepared_api import _origin, _session

router = APIRouter(prefix='/api')


def _model_id(value):
    from .machine_profiles import model_id
    return model_id(value) if value else None


def _active_task_ids():
    found = []
    for project in core.list_projects():
        for path in (core.project_path(project['id']) / 'Print').glob('*/job.json'):
            job = core.read_json(path, {})
            if job.get('state') in bambuddy.ACTIVE_JOB_STATES:
                found.append(f"{project['id']}/{path.parent.name}")
    return found


@router.get('/printers')
def printers(request: Request):
    _origin(request)
    try:
        found = onboarding.discover()
    except Exception:  # discovery must never break the page; report that the list is unavailable
        raise HTTPException(503, '无法从 Bambuddy 读取打印机列表') from None
    current = core.config().get('printer_id')
    card = core.read_json(core.safe_path(core.ROOT, workspace.DOCUMENTS['device'], require_file=False), {})
    card_model = (card.get('user_confirmed') or {}).get('model') or card.get('model')
    rows = []
    for row in found['printers']:
        rows.append({**row, 'selected': row.get('id') == current,
                     'matches_device_card': (_model_id(card_model) == _model_id(row.get('model'))) if card_model else None})
    return {'printers': rows, 'selected_printer_id': current, 'device_card_model': card_model,
            'active_tasks': _active_task_ids()}


class Select(BaseModel):
    model_config = ConfigDict(extra='forbid')
    printer_id: StrictInt = Field(ge=1, le=1_000_000)


@router.post('/printer/select')
def select(body: Select, request: Request):
    _session(request, write=True)
    with core.lock('printer'):
        active = _active_task_ids()
        if active:
            raise HTTPException(409, '有进行中或结果不明的打印任务，先核对完再切换设备：' + '、'.join(active[:3]))
        try:
            listed = onboarding.discover()['printers']
        except Exception:
            raise HTTPException(503, '无法从 Bambuddy 读取打印机列表，未切换') from None
        target = next((p for p in listed if p.get('id') == body.printer_id), None)
        if target is None:
            raise HTTPException(404, 'Bambuddy 里没有这台打印机')
        current = workspace.connection()
        if current['printer_id'] == body.printer_id:
            return {'changed': False, 'printer_id': body.printer_id, 'message': '已经是这台设备'}
        try:
            workspace.configure_connection(current['bambuddy_url'], body.printer_id, current['sha256'])
        except core.WorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc
    card = core.read_json(core.safe_path(core.ROOT, workspace.DOCUMENTS['device'], require_file=False), {})
    card_model = (card.get('user_confirmed') or {}).get('model') or card.get('model')
    mismatch = bool(card_model) and _model_id(card_model) != _model_id(target.get('model'))
    return {'changed': True, 'printer_id': body.printer_id, 'printer': target, 'device_card_model': card_model,
            'device_card_mismatch': mismatch,
            'message': ('已切换。设备卡仍记录为 ' + str(card_model) + '，与这台 ' + str(target.get('model'))
                        + ' 不一致，设备卡里的供料与喷嘴信息不适用于它；已有的切片与批准需要重新生成。') if mismatch
                       else '已切换。已有的切片与批准需要重新生成。'}
