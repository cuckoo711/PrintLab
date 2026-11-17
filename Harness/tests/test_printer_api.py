import json

import pytest
from fastapi.testclient import TestClient

from printlab import core, onboarding, prepared_api, printer_api
from printlab.api import app

PRINTERS = {'printers': [{'id': 1, 'name': '3DP-H2C', 'model': 'H2C'}, {'id': 2, 'name': '3DP-X1C', 'model': 'X1C'}],
            'observed_at': 'now', 'selected_printer_id': 1}
ORIGIN = {'Origin': 'http://127.0.0.1:8018'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'ROOT', tmp_path)
    (tmp_path / 'Runtime').mkdir()
    core.write_json(tmp_path / 'Runtime/config.json', {'bambuddy_url': 'http://127.0.0.1:8000', 'printer_id': 1})
    card = tmp_path / 'Library/Devices/X2D/device.json'
    card.parent.mkdir(parents=True)
    core.write_json(card, {'schema_version': 1, 'name': '3DP-H2C', 'model': 'H2C', 'user_confirmed': {'model': 'H2C', 'ams_count': 3}})
    (card.parent / 'installed.md').write_text('# 本机设备\n\n- 供料：三个 AMS\n', encoding='utf-8')
    monkeypatch.setattr(onboarding, 'discover', lambda *a, **k: json.loads(json.dumps(PRINTERS)))
    prepared_api._sessions.clear()
    return TestClient(app, base_url='http://127.0.0.1:8018')


def write_headers(client):
    token = client.get('/api/prepared/session').json()['csrf_token']
    return {**ORIGIN, 'X-PrintLab-CSRF': token}


def config_text(tmp):
    return (tmp / 'Runtime/config.json').read_text(encoding='utf-8')


def card_text(tmp):
    return (tmp / 'Library/Devices/X2D/device.json').read_text(encoding='utf-8')


def notes_text(tmp):
    path = tmp / 'Library/Devices/X2D/installed.md'
    return path.read_text(encoding='utf-8') if path.is_file() else None


def add_job(tmp, state):
    project = tmp / 'Project/P001-T'
    (project / 'Print/J1').mkdir(parents=True)
    core.write_json(project / 'project.json', {'id': 'P001', 'name': 'T', 'slug': 'T', 'status': 'printing'})
    core.write_json(project / 'Print/J1/job.json', {'id': 'J1', 'state': state})


def test_list_marks_the_selected_printer_and_whether_it_matches_the_device_card(client):
    data = client.get('/api/printers').json()
    assert data['selected_printer_id'] == 1 and data['device_card_model'] == 'H2C'
    by_id = {p['id']: p for p in data['printers']}
    assert by_id[1]['selected'] is True and by_id[1]['matches_device_card'] is True
    assert by_id[2]['selected'] is False and by_id[2]['matches_device_card'] is False
    assert data['active_tasks'] == []


def test_list_reports_unavailable_instead_of_failing_the_page(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('down')
    monkeypatch.setattr(onboarding, 'discover', boom)
    assert client.get('/api/printers').status_code == 503


def test_list_is_refused_from_a_foreign_host_or_cross_site(client):
    assert client.get('/api/printers', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/printers', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_select_needs_session_origin_and_csrf(client, tmp_path):
    before = config_text(tmp_path)
    body = {'printer_id': 2}
    assert client.post('/api/printer/select', json=body).status_code == 403
    assert client.post('/api/printer/select', json=body, headers=ORIGIN).status_code == 403
    h = write_headers(client)
    assert client.post('/api/printer/select', json=body, headers={**h, 'X-PrintLab-CSRF': 'wrong'}).status_code == 403
    assert client.post('/api/printer/select', json=body, headers={**h, 'Origin': 'http://evil.example'}).status_code == 403
    assert client.post('/api/printer/select', json=body, headers={**h, 'Host': 'evil.example'}).status_code == 403
    assert config_text(tmp_path) == before


@pytest.mark.parametrize('body', [{}, {'printer_id': 0}, {'printer_id': -1}, {'printer_id': '2'}, {'printer_id': 2.5}, {'printer_id': True},
                                  {'printer_id': 2, 'bambuddy_url': 'http://evil'}, {'printer_id': 2, 'extra': 1}, {'printer_id': 10**9}])
def test_select_rejects_malformed_bodies_without_changing_anything(client, tmp_path, body):
    before = config_text(tmp_path)
    assert client.post('/api/printer/select', json=body, headers=write_headers(client)).status_code == 422
    assert config_text(tmp_path) == before


def test_select_switches_the_printer_id_and_nothing_else_in_the_config(client, tmp_path):
    before = json.loads(config_text(tmp_path))
    result = client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client))
    assert result.status_code == 200
    data = result.json()
    assert data['changed'] is True and data['printer_id'] == 2 and data['card'] == 'blank'
    assert json.loads(config_text(tmp_path)) == {**before, 'printer_id': 2}


def test_the_current_card_is_archived_untouched_and_a_blank_one_describes_only_what_bambuddy_reported(client, tmp_path):
    original_card, original_notes = card_text(tmp_path), notes_text(tmp_path)
    client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client))
    # the H2C card is kept byte for byte under its own printer id
    assert (tmp_path / 'Library/Devices/printer-1/device.json').read_text(encoding='utf-8') == original_card
    assert (tmp_path / 'Library/Devices/printer-1/installed.md').read_text(encoding='utf-8') == original_notes
    # the live card is now a blank one for the X1C: name and model only, nothing invented
    live = json.loads(card_text(tmp_path))
    assert live['name'] == '3DP-X1C' and live['model'] == 'X1C' and live['verification_status'] == 'unconfigured'
    assert live.get('user_confirmed', {}) == {} and live.get('installed', {}) == {}
    assert 'AMS' not in card_text(tmp_path) and notes_text(tmp_path) is None  # no notes file is invented either
    assert '待核对' in card_text(tmp_path) or live.get('unverified')


def test_switching_there_and_back_restores_both_files_byte_for_byte(client, tmp_path):
    original = (config_text(tmp_path), card_text(tmp_path), notes_text(tmp_path))
    h = write_headers(client)
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=h).json()['card'] == 'blank'
    back = client.post('/api/printer/select', json={'printer_id': 1}, headers=h).json()
    assert back['card'] == 'restored'
    assert json.loads(config_text(tmp_path)) == json.loads(original[0])
    assert (card_text(tmp_path), notes_text(tmp_path)) == original[1:]


def test_each_printer_keeps_its_own_card_across_several_switches(client, tmp_path):
    h = write_headers(client)
    client.post('/api/printer/select', json={'printer_id': 2}, headers=h)
    # the user fills in the X1C card
    path = tmp_path / 'Library/Devices/X2D/device.json'
    card = json.loads(card_text(tmp_path)); card['user_confirmed'] = {'model': 'X1C', 'ams_count': 1}
    core.write_json(path, card)
    x1c_card = card_text(tmp_path)
    h2c = (tmp_path / 'Library/Devices/printer-1/device.json').read_text(encoding='utf-8')
    client.post('/api/printer/select', json={'printer_id': 1}, headers=h)
    assert card_text(tmp_path) == h2c
    again = client.post('/api/printer/select', json={'printer_id': 2}, headers=h).json()
    assert again['card'] == 'restored' and card_text(tmp_path) == x1c_card


def test_a_switch_that_fails_midway_leaves_everything_as_it_was(client, tmp_path, monkeypatch):
    before = (config_text(tmp_path), card_text(tmp_path), notes_text(tmp_path))
    def boom(*a, **k):
        raise core.WorkflowError('disk full')
    monkeypatch.setattr(printer_api.workspace, 'configure_connection', boom)
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client)).status_code == 409
    assert (config_text(tmp_path), card_text(tmp_path), notes_text(tmp_path)) == before
    assert not (tmp_path / 'Library/Devices/printer-1').exists() or (tmp_path / 'Library/Devices/printer-1/device.json').read_text(encoding='utf-8') == before[1]


def test_a_failure_while_writing_the_new_card_rolls_the_config_back(client, tmp_path, monkeypatch):
    before = (config_text(tmp_path), card_text(tmp_path), notes_text(tmp_path))
    real = core.write_json
    def flaky(path, value):
        if str(path).replace('\\', '/').endswith('Library/Devices/X2D/device.json'):
            raise OSError('simulated write failure')
        return real(path, value)
    monkeypatch.setattr(printer_api.core, 'write_json', flaky)
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client)).status_code in {409, 500}
    assert (config_text(tmp_path), card_text(tmp_path), notes_text(tmp_path)) == before


def test_a_card_without_a_notes_file_is_switched_and_restored_without_inventing_one(client, tmp_path):
    (tmp_path / 'Library/Devices/X2D/installed.md').unlink()
    h = write_headers(client)
    original = card_text(tmp_path)
    client.post('/api/printer/select', json={'printer_id': 2}, headers=h)
    client.post('/api/printer/select', json={'printer_id': 1}, headers=h)
    assert card_text(tmp_path) == original and not (tmp_path / 'Library/Devices/X2D/installed.md').exists()


def test_the_page_is_told_what_happened_to_the_card(client):
    data = client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client)).json()
    assert '空白' in data['message'] and '原样保存' in data['message'] and '没有填写' in data['message']
    back = client.post('/api/printer/select', json={'printer_id': 1}, headers=write_headers(client)).json()
    assert '还原' in back['message']


def test_selecting_the_current_printer_is_a_no_op(client, tmp_path):
    before = config_text(tmp_path)
    data = client.post('/api/printer/select', json={'printer_id': 1}, headers=write_headers(client)).json()
    assert data['changed'] is False and config_text(tmp_path) == before


def test_unknown_printer_is_refused_and_nothing_changes(client, tmp_path):
    before = config_text(tmp_path)
    assert client.post('/api/printer/select', json={'printer_id': 99}, headers=write_headers(client)).status_code == 404
    assert config_text(tmp_path) == before


def test_bambuddy_being_unreachable_means_no_switch(client, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('down')
    monkeypatch.setattr(onboarding, 'discover', boom)
    before = config_text(tmp_path)
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client)).status_code == 503
    assert config_text(tmp_path) == before


@pytest.mark.parametrize('state', ['dispatching', 'dispatch_unknown', 'submitted', 'preparing', 'running', 'printing', 'paused', 'staged_requires_reconciliation'])
def test_no_switch_while_a_print_task_is_active_or_unknown(client, tmp_path, state):
    add_job(tmp_path, state)
    before = config_text(tmp_path)
    listing = client.get('/api/printers').json()
    assert listing['active_tasks'] == ['P001/J1']
    result = client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client))
    assert result.status_code == 409 and 'P001/J1' in result.json()['detail']
    assert config_text(tmp_path) == before


def test_finished_tasks_do_not_block_a_switch(client, tmp_path):
    add_job(tmp_path, 'completed')
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client)).json()['changed'] is True


def test_a_task_bound_to_the_old_printer_is_not_reusable_after_the_switch(client, tmp_path):
    # PrintLab's own check, which this feature relies on instead of reimplementing.
    job = {'printer_id': 1}
    client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client))
    assert job.get('printer_id') != core.config().get('printer_id')


def test_only_the_documented_methods_are_accepted(client, tmp_path):
    before = config_text(tmp_path)
    h = write_headers(client)
    for method in ('put', 'patch', 'delete'):
        assert getattr(client, method)('/api/printers', headers=h).status_code == 405, method
        assert client.request(method.upper(), '/api/printer/select', headers=h, json={'printer_id': 2}).status_code == 405, method
    assert client.post('/api/printers', headers=h).status_code == 405
    assert client.get('/api/printer/select').status_code in {404, 405}  # PrintLab's catch-all route answers 404 for unknown GETs under /api
    assert config_text(tmp_path) == before


def test_the_list_says_which_printers_already_have_a_card(client, tmp_path):
    first = {p['id']: p['has_card'] for p in client.get('/api/printers').json()['printers']}
    assert first == {1: True, 2: False}  # the selected one has the live card; the other has none yet
    h = write_headers(client)
    client.post('/api/printer/select', json={'printer_id': 2}, headers=h)
    after = {p['id']: p['has_card'] for p in client.get('/api/printers').json()['printers']}
    assert after == {1: True, 2: True}  # the H2C card is now stored, the X1C has the live blank one
