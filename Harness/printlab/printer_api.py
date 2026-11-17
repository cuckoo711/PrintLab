"""Choose which Bambuddy printer the workbench is bound to.

Same rules as the other browser controls: same origin, a session cookie and a CSRF header on every change.
Switching rewrites ``printer_id`` in Runtime/config.json through ``workspace.configure_connection`` and swaps the
device card to the one that belongs to the chosen printer. Each printer keeps its own card under
``Library/Devices/printer-<id>/``: the card in use is stored there byte for byte before the swap, and switching back
puts the stored card back byte for byte. A printer with no stored card gets a blank one that holds only the name and
model Bambuddy reported, with the facts to confirm left empty; nothing about feeding or nozzles is ever made up.
Credentials, projects and print tasks are not touched, and nothing is ever sent.

If any step fails, every file is put back as it was, so a failed switch leaves no half-changed state.

A switch is refused while a print task is active or its outcome is unknown, and when the chosen printer is not
one Bambuddy lists. Approvals and slices already carry a printer id; they are checked against the current
printer when they are used, so after a switch they ask for a new slice instead of being reused silently.
"""
import shutil

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
        stored = _store_dir(row['id']) / 'device.json' if isinstance(row.get('id'), int) else None
        rows.append({**row, 'selected': row.get('id') == current,
                     # a printer that is not selected has a card only if one was stored when it was switched away from
                     'has_card': bool(row.get('id') == current or (stored is not None and stored.is_file())),
                     'matches_device_card': (_model_id(card_model) == _model_id(row.get('model'))) if card_model else None})
    return {'printers': rows, 'selected_printer_id': current, 'device_card_model': card_model,
            'active_tasks': _active_task_ids()}


class Select(BaseModel):
    model_config = ConfigDict(extra='forbid')
    printer_id: StrictInt = Field(ge=1, le=1_000_000)


CARD_FILES = ('device.json', 'installed.md')


def _live_dir():
    return core.safe_path(core.ROOT, 'Library/Devices/X2D', require_file=False)


def _store_dir(printer_id):
    return core.safe_path(core.ROOT, f'Library/Devices/printer-{int(printer_id)}', require_file=False)


def _snapshot():
    """Everything a switch can change, kept in memory so a failure can put it all back."""
    live = _live_dir()
    return {
        'config': (core.ROOT / 'Runtime/config.json').read_bytes() if (core.ROOT / 'Runtime/config.json').is_file() else None,
        'live': {name: (live / name).read_bytes() if (live / name).is_file() else None for name in CARD_FILES},
    }


def _restore(snapshot, stored_dir, created_store):
    live = _live_dir()
    config = core.ROOT / 'Runtime/config.json'
    if snapshot['config'] is not None:
        config.write_bytes(snapshot['config'])
    live.mkdir(parents=True, exist_ok=True)
    for name, data in snapshot['live'].items():
        path = live / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)
    if created_store and stored_dir.exists():
        shutil.rmtree(stored_dir, ignore_errors=True)


def _blank_card(target):
    return {'schema_version': 1, 'name': target.get('name'), 'model': target.get('model'),
            'verification_status': 'unconfigured', 'user_confirmed': {}, 'installed': {},
            'unverified': ['供料、喷嘴、打印板与材料都还没有核对；这张卡只记录了 Bambuddy 报告的名称和机型']}


def _swap_cards(current_id, target, target_id):
    """Put the current printer's card away and bring out the target's. Returns 'restored' or 'blank'."""
    live = _live_dir()
    away = _store_dir(current_id) if current_id is not None else None
    created_store = bool(away) and not away.exists()
    incoming = _store_dir(target_id)
    has_stored = incoming.is_dir() and (incoming / 'device.json').is_file()
    if away is not None:
        away.mkdir(parents=True, exist_ok=True)
        for name in CARD_FILES:
            source = live / name
            if source.is_file():
                (away / name).write_bytes(source.read_bytes())
            else:
                (away / name).unlink(missing_ok=True)
    if has_stored:
        for name in CARD_FILES:
            stored = incoming / name
            if stored.is_file():
                core.safe_path(core.ROOT, f'Library/Devices/X2D/{name}', require_file=False).write_bytes(stored.read_bytes())
            else:
                (live / name).unlink(missing_ok=True)
        return 'restored', created_store
    (live / 'installed.md').unlink(missing_ok=True)
    core.write_json(live / 'device.json', _blank_card(target))
    return 'blank', created_store


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
        snapshot = _snapshot()
        created_store = False
        try:
            with core.lock('shared-context'):
                outcome, created_store = _swap_cards(current['printer_id'], target, body.printer_id)
            workspace.configure_connection(current['bambuddy_url'], body.printer_id, current['sha256'])
        except HTTPException:
            raise
        except Exception as exc:  # any failure, including disk errors: put every file back
            _restore(snapshot, _store_dir(current['printer_id']) if current['printer_id'] is not None else None, created_store)
            if isinstance(exc, core.WorkflowError):
                raise HTTPException(409, str(exc)) from exc
            raise HTTPException(500, '切换失败，已全部还原，设备没有变化') from None
    name, model = target.get('name'), target.get('model')
    if outcome == 'restored':
        message = f'已切换到 {name}，并还原了它之前保存的设备卡。已有的切片与批准需要重新生成。'
    else:
        message = (f'已切换到 {name}（{model}）。它还没有设备卡，已建一张空白的，只记录了名称和机型；'
                   f'供料、喷嘴等信息没有填写，需要你核对后补上。原来那台的设备卡已原样保存，切回去会还原。'
                   f'已有的切片与批准需要重新生成。')
    return {'changed': True, 'printer_id': body.printer_id, 'printer': target, 'card': outcome, 'message': message}
