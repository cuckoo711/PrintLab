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
    core.write_json(card, {'schema_version': 1, 'name': '3DP-H2C', 'model': 'H2C', 'user_confirmed': {'model': 'H2C'}})
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


def test_select_switches_only_the_printer_id(client, tmp_path):
    before = json.loads(config_text(tmp_path))
    card = card_text(tmp_path)
    result = client.post('/api/printer/select', json={'printer_id': 2}, headers=write_headers(client))
    assert result.status_code == 200
    data = result.json()
    assert data['changed'] is True and data['printer_id'] == 2 and data['device_card_mismatch'] is True
    assert 'H2C' in data['message'] and 'X1C' in data['message'] and '重新生成' in data['message']
    after = json.loads(config_text(tmp_path))
    assert after == {**before, 'printer_id': 2}  # nothing else in the config moved
    assert card_text(tmp_path) == card  # the device card is left exactly as the user wrote it


def test_select_back_restores_the_original_config_byte_for_byte(client, tmp_path):
    original = config_text(tmp_path)
    h = write_headers(client)
    assert client.post('/api/printer/select', json={'printer_id': 2}, headers=h).json()['changed'] is True
    assert client.post('/api/printer/select', json={'printer_id': 1}, headers=h).json()['device_card_mismatch'] is False
    assert json.loads(config_text(tmp_path)) == json.loads(original)


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
