"""Separate group routing, without real credentials or external delivery."""
import pytest

from tests.test_research_delivery_api import client, PREFIX
from tests.test_research_delivery_aggregation import research
from tests.test_research_delivery_outbox import enabled_target
from src.notification_sender.feishu_sender import DeliveryOutcome


def test_every_category_can_bind_a_separate_group(client):
    response = client.get(PREFIX + '/categories')
    assert response.status_code == 200
    categories = response.json()['items']
    assert len(categories) == 14
    assert sum(item['preview_available'] for item in categories) == 10
    destinations = []
    for category in categories:
        target = client.post(PREFIX + '/destinations', json={
            'alias': category['display_name'], 'kind': category['destination_kind'],
        }).json()
        result = client.post(PREFIX + '/subscriptions', json={
            'name': category['display_name'], 'strategy_id': category['category_id'],
            'destination_id': target['id'], 'planned_time': category['planned_time'],
        })
        assert result.status_code == 201
        assert result.json()['enabled'] is False
        destinations.append(result.json()['destination_id'])
        if not category['preview_available']:
            preview = client.post(PREFIX + '/subscriptions/' + result.json()['id'] + '/preview', json={'source_run_id': 'fake'})
            assert preview.status_code == 409
            assert preview.json()['error'] == 'category_preview_unavailable'
    assert len(set(destinations)) == 14
    assert len(client.get(PREFIX + '/strategies').json()['items']) == 10
    assert client.get(PREFIX + '/capabilities').json()['scheduler_active'] is False


@pytest.mark.parametrize('kind', ['test', 'strategy', 'radar', 'ops'])
def test_each_group_can_explicitly_test_its_own_connection(research, kind):
    publisher, _, dest = research
    dest = publisher.service.update_destination(dest['id'], {'version': dest['version'], 'kind': kind})
    dest = enabled_target(publisher, dest)
    calls = []
    publisher.sender = lambda creds, payload: calls.append((creds, payload)) or DeliveryOutcome('sent', None)
    result = publisher.test(dest['id'], dest['version'], True, 'per-group-test')
    publisher.test(dest['id'], dest['version'], True, 'per-group-test')
    assert result['destination_id'] == dest['id'] and result['status'] == 'sent'
    assert len(calls) == 1
    assert calls[0][1]['content']['text'] == publisher.capabilities()['test_message']
