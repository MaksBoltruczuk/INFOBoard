import asyncio
from collections import defaultdict
import logging
import uuid
from asyncio import gather
from copy import deepcopy
from typing import Any, Dict, List, Sequence

from channels.db import database_sync_to_async
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from faker import Faker

from draw.utils import (dump_content, user_id_for_room, user_is_authenticated, user_is_authorized,
                        user_is_staff)
from draw.utils.django_loaded import LoggingAsyncJsonWebsocketConsumer
from ltiapi.models import CustomUser

from . import models as m

logger = logging.getLogger("draw.collab")

create_record = database_sync_to_async(m.ExcalidrawLogRecord.objects.create)
bulk_create_records = database_sync_to_async(m.ExcalidrawLogRecord.objects.bulk_create)
upsert_room = database_sync_to_async(m.ExcalidrawRoom.objects.update_or_create)
get_or_create_room = database_sync_to_async(m.ExcalidrawRoom.objects.get_or_create)
auth_room = database_sync_to_async(
    m.ExcalidrawRoom.objects.only("room_name", "room_consumer").get_or_create)

@database_sync_to_async
def user_name(user):
    return user.username

class CollaborationConsumer(LoggingAsyncJsonWebsocketConsumer):
    allowed_eventtypes = {'collaborator_change', 'elements_changed', 'save_room', 'full_sync'}
    channel_prefix = 'draw_room_'

    @property
    def group_name(self):
        return self.channel_prefix + self.room_name

    async def connect(self):
        url_route: dict = self.scope.get('url_route')
        # pylint: disable=attribute-defined-outside-init
        self.args: Sequence[Any] = url_route.get('args')
        self.kwargs: Dict[str, Any] = url_route.get('kwargs')

        self.user: CustomUser = self.scope.get('user')
        self.room_name = self.kwargs.get('room_name')
        room, _ = await auth_room(room_name=self.room_name)

        authenticated, authorized = await asyncio.gather(
            user_is_authenticated(self.user),
            user_is_authorized(self.user, room))
        if not settings.ALLOW_ANONYMOUS_VISITS and not authenticated and not authorized:
            _, username = await asyncio.gather(
                super().connect(),
                user_name(self.user)
            )
            who = 'Someone' if not authenticated else username
            reason = (
                'anonymous visits are disallowed.'
                if not authenticated
                else 'this user is not allowed to access the room.')
            logger.warning(
                '%(who)s tried to enter room %(room)s without logging in, but %(reason)s',
                {'who': who, 'room': self.room_name, 'reason': reason})
            await self.send_json({'eventtype': 'login_required'})
            return await self.disconnect(3000)

        self.user_room_id = self.user.id_for_room(self.room_name) \
            if self.user.id is not None \
            else user_id_for_room(uuid.uuid4(), self.room_name)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        return await super().connect()

    async def disconnect(self, code):
        """
        Notify all collaborators if a client left, so they
        can remove it from their collaborator list, too.
        """
        if code != 3000:
            await self.send_event(
                'collaborator_left', collaborator={'userRoomId': self.user_room_id})
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        return await super().disconnect(code)

    async def collaborator_change(self, room_name, eventtype, changes: List[dict], **kwargs):
        """
        Forwards all updates to users and their pointers to clients and logs them to the data base.
        """
        # logger.debug("called collaborator_change")
        collaborator_to_send = deepcopy(changes[-1])
        records = []
        for change in changes:
            del change['username']
            time = change.pop('time', None)
            time = parse_datetime(time) if time else now()
            record = m.ExcalidrawLogRecord(
                room_name=room_name,
                event_type=eventtype,
                user_pseudonym=self.user_room_id,
                created_at=time
            )
            record.content = change
            records.append(record)

        collaborator_to_send['userRoomId'] = self.user_room_id
        await gather(
            bulk_create_records(records),
            self.send_event(eventtype, changes=[collaborator_to_send]))

    async def full_sync(self, room_name, eventtype, elements, **kwargs):
        """
        Forwards all full syncs to clients, logs them to the data base ~~and saves the room~~.
        """
        await self.elements_changed(room_name, eventtype, elements=elements, **kwargs)
        # await gather(
        #     self.elements_changed(room_name, eventtype, elements=elements, **kwargs),
        #     self.save_room(room_name, elements, **kwargs))

    async def elements_changed(self, room_name, eventtype, elements, **kwargs):
        """
        Forwards all full syncs and single edits to clients and logs them to the data base.
        """
        record = m.ExcalidrawLogRecord(
            room_name=room_name,
            event_type=eventtype,
            user_pseudonym=self.user_room_id
        )
        record.content = elements
        await gather(
            self.send_event(eventtype, elements=elements, **kwargs),
            database_sync_to_async(record.save)())

    async def save_room(self, room_name, elements, **kwargs):
        """
        Saves the room if all submitted elements have a newser version than the saved version.

        If a submitted element happens to have an older version number than an already stored
        version of the element, nothing will be done. It is assumed, taht the clients submit
        storage requests often enogh so that not too much data will be lost if this happens.
        This is because the author wants the element reconciliation always to be executed on
        the client side and not both, the client and the server. The clients should instead
        ensure that a ``full_sync`` happens often enough.

        Deleted elements will not be saved.
        """
        old_room, created = await get_or_create_room(room_name=room_name)
        old_room_versions = {e['id']: e['version'] for e in old_room.elements}
        elements = [e for e in elements if not e.get('isDeleted', False)]

        differences_detected = False

        if not created:
            for e in elements:
                old_version = old_room_versions.get(e['id'], -1)
                if old_version > e['version']:
                    return
                # no difference if version is equal.
                differences_detected = differences_detected or old_version < e['version']

        if not differences_detected:
            return

        elements, _ = dump_content(elements)
        room, _ = await upsert_room(
            room_name=room_name,
            defaults={'_elements': elements})
        logger.debug("room %s saved", room.room_name)

    async def send_event(self, eventtype, **event_args):
        """
        Helper to forward messages to other clients using channel layers.
        """
        await self.channel_layer.group_send(self.group_name, {
            'type': 'notify_client',
            'notification': {
                'eventtype': eventtype,
                **event_args
            },
            'sender': self.channel_name
        })

    async def notify_client(self, event: dict):
        """
        Receives broadcast commissions for notifying clients.
        """
        # dont't send the event back to the sender
        if event['sender'] != self.channel_name:
            await self.send_json(event['notification'])


get_log_record = database_sync_to_async(m.ExcalidrawLogRecord.objects.get)

@database_sync_to_async
def get_log_record_ids_for_room(room_name):
    return [val[0] for val in m.ExcalidrawLogRecord.objects
        .filter(room_name=room_name)
        .order_by('id')
        .values_list('id')]

class ReplayConsumer(LoggingAsyncJsonWebsocketConsumer):
    # pylint: disable=attribute-defined-outside-init
    allowed_eventtypes = {'start_replay', 'pause_replay', 'restart_replay'}

    async def connect(self):
        self.user: CustomUser = self.scope.get('user')
        if not await user_is_staff(self.user):
            return await self.disconnect(3000)

        url_route: dict = self.scope.get('url_route')
        self.args: Sequence[Any] = url_route.get('args')
        self.kwargs: Dict[str, Any] = url_route.get('kwargs')
        self.room_name = self.kwargs.get('room_name')


        # this will be fun :)
        self.faker = Faker()
        self.encountered_user_pseudonyms = defaultdict(self.faker.name)

        await super().connect()
        await asyncio.sleep(0.1)
        await self.restart_replay()

    async def receive_json(self, content, *args, **kwargs):
        """
        Discards all received messages.
        """
        try:
            await super().receive_json(content, *args, **kwargs)
        except ValueError as e:
            logger.debug(e)

    async def disconnect(self, code):
        if hasattr(self, 'replay_task'):
            logger.debug('client disconnected before replay of room %s finished.', self.room_name)
            self.replay_task.cancel()
        return await super().disconnect(code)

    async def init_replay(self):
        self.log_record_ids, _ = await asyncio.gather(
            get_log_record_ids_for_room(room_name=self.room_name),
            self.send_json({'eventtype': 'reset_scene'})
        )

    async def start_replay(self, *args, **kwargs):
        logger.info('start replay mode for room %s', self.room_name)
        if not self.log_record_ids:
            await self.init_replay()
        self.replay_task = asyncio.create_task(self.wait_then_send())
        await self.send_json({'eventtype': 'start_replay'})

    async def pause_replay(self, *args, **kwargs):
        if hasattr(self, 'replay_task'):
            logger.debug('replay for room %s paused.', self.room_name)
            self.replay_task.cancel()
            await self.send_json({'eventtype': 'pause_replay'})

    async def restart_replay(self, *args, **kwargs):
        if hasattr(self, 'replay_task'):
            self.replay_task.cancel()
        await self.init_replay()
        await self.start_replay()

    async def send_next_event(self):
        log_id = self.log_record_ids.pop(0)
        record: m.ExcalidrawLogRecord = await get_log_record(pk=log_id)
        if record.event_type in ['full_sync', 'elements_changed']:
            await self.send_json({
                'eventtype': record.event_type,
                'elements': record.content,
            })
        elif record.event_type == 'collaborator_change':
            await self.send_json({
                'eventtype': 'collaborator_change',
                'changes': [{
                    **record.content,
                    'username': self.encountered_user_pseudonyms[record.user_pseudonym],
                    'userRoomId': record.user_pseudonym,
                }]
            })

    async def wait_then_send(self):
        if self.log_record_ids:
            # FIXME: this shield will not work. still need to understand semantics of shield!!!
            #        another way would be to wait for a task and then just don't start another one.
            await asyncio.shield(self.send_next_event())
            await asyncio.sleep(settings.BROADCAST_RESOLUTION / 1000)
            self.replay_task = asyncio.create_task(self.wait_then_send())
        else:
            await self.send_json({'eventtype': 'pause_replay'})
