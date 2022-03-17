import asyncio
import logging
from typing import List, Optional, cast

import aiohttp
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model, login
from django.http import (HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden,
                         JsonResponse)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import classonlymethod
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView
from pylti1p3.contrib.django import DjangoCacheDataStorage, DjangoMessageLaunch, DjangoOIDCLogin
from pylti1p3.contrib.django.lti1p3_tool_config import DjangoDbToolConf
from pylti1p3.deep_link_resource import DeepLinkResource

from collab.models import ExcalidrawRoom
from draw.utils import absolute_reverse, make_room_name
from draw.utils.auth import user_is_authorized

from . import models as m
from .utils import (get_course_context, get_course_id, get_custom_launch_data, get_ext_data,
                    get_launch_url, get_lti_tool, get_room_name, get_user_room_name,
                    issuer_namespaced_username, lti_registration_data,
                    make_tool_config_from_openid_config_via_link)

logger = logging.getLogger("draw.ltiapi")


class RegisterConsumerView(DetailView):
    """
    This View implements LTI Advantage Automatic registration. It supports GET for the user
    to control the configuration steps and POST, which starts the consumer configuration.
    """
    template_name = 'ltiapi/register_consumer_start.html'
    end_template_name = 'ltiapi/register_consumer_result.html'
    model = m.OneOffRegistrationLink
    context_object_name = 'link'

    def get_template_names(self) -> List[str]:
        if self.request.method == 'POST':
            return [self.end_template_name]
        return [self.template_name]

    @classonlymethod
    def as_view(cls, **initkwargs):
        # this needs to be "hacked", so that the class view supports async views.
        view = super().as_view(**initkwargs)
        # pylint: disable=protected-access
        view._is_coroutine = asyncio.coroutines._is_coroutine
        return view

    # pylint: disable = invalid-overridden-method, attribute-defined-outside-init
    async def get(self, request: HttpRequest, *args, **kwargs):
        return await sync_to_async(super().get)(request, *args, **kwargs) # type: ignore

    async def post(self, request: HttpRequest, *args, **kwargs):
        """
        Register the application at the provider via the LTI registration flow.

        The configuration flow is well explained at https://moodlelti.theedtech.dev/dynreg/
        """
        # verify that the registration link is unused
        self.object = reg_link = await sync_to_async(self.get_object)() # type: ignore
        if reg_link.registered_consumer is not None:
            ctx = {'error': _(
                'The registration link has already been used. Please ask '
                'the admin of the LTI app for a new registration link.')}
            return self.render_to_response(context=ctx)

        # prepare for getting data about the consumer
        openid_config_endpoint = request.GET.get('openid_configuration')
        jwt_str = request.GET.get('registration_token')

        async with aiohttp.ClientSession() as session:
            # get information about how to register to the consumer
            logger.info('Getting registration data from "%s"', openid_config_endpoint)
            resp = await session.get(openid_config_endpoint)
            openid_config = await resp.json()

            # send registration to the consumer
            tool_provider_registration_endpoint = openid_config['registration_endpoint']
            registration_data = lti_registration_data(request)
            logger.info('Registering tool at "%s"', tool_provider_registration_endpoint)
            resp = await session.post(
                tool_provider_registration_endpoint,
                json=registration_data,
                headers={
                    'Authorization': 'Bearer ' + jwt_str,
                    'Accept': 'application/json'
                })
            openid_registration = await resp.json()
        try:
            # use the information about the registration to regsiter the consumer to this app
            consumer = await make_tool_config_from_openid_config_via_link(
                openid_config, openid_registration, reg_link)
        except AssertionError as e:
            # error if the data from the consumer is missing mandatory information
            ctx = self.get_context_data(registration_success=False, error=e)
            return self.render_to_response(ctx, status=406)

        await sync_to_async(reg_link.registration_complete)(consumer)

        logging.info(
            'Registration of issuer "%s" with client %s complete',
            consumer.issuer, consumer.client_id)
        ctx = self.get_context_data(registration_success=True)
        return self.render_to_response(ctx)


async def oidc_jwks(
    request: HttpRequest, issuer: Optional[str] = None,
    client_id: Optional[str] = None
):
    """ JWT signature delivery endpoint """
    tool_conf = DjangoDbToolConf()
    get_jwks = sync_to_async(tool_conf.get_jwks)
    return JsonResponse(await get_jwks(issuer, client_id))


def oidc_login(request: HttpRequest):
    """
    This just verifies that the requesting consumer is allowed to log in and tells the consumer,
    where to go next. The actual user login happens when the LTI launch is performed.
    """
    tool_conf = DjangoDbToolConf()
    launch_data_storage = DjangoCacheDataStorage()

    oidc_login_handle = DjangoOIDCLogin(request, tool_conf, launch_data_storage=launch_data_storage)
    target_link_uri = get_launch_url(request)
    oidc_redirect = oidc_login_handle\
        .enable_check_cookies()\
        .redirect(target_link_uri, False)
    return oidc_redirect

oidc_login.csrf_exempt = True # type: ignore
oidc_login.xframe_options_exempt = True # type: ignore
# XXX: what caould go wrong if an attacker would trigger the login
#      for a known issuer? could they somehow change a deeplink?
# YYY: → is the OIDC login flow csrf-safe? there is no possibility
#      to pass a valid csrf token by the LTI Platform though...
# TODO: it would be nice to have the referrer check logic from the
#       csrf middleware here so at least this could be checked.

@require_POST
def lti_configure(request: HttpRequest, launch_id: str):
    tool_conf = DjangoDbToolConf()
    launch_data_storage = DjangoCacheDataStorage()
    message_launch = DjangoMessageLaunch.from_cache(
        launch_id, request, tool_conf, launch_data_storage=launch_data_storage)
    message_launch_data = cast(dict, message_launch.get_launch_data())
    lti_tool = get_lti_tool(tool_conf, message_launch_data)

    # create a deep link and initialize the room
    course_title = get_course_context(message_launch_data).get('title', None)
    title = message_launch_data\
        .get('https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings', {})\
        .get('title', course_title)
    title = "Draw Together" + (f" – {title}" if title else "")

    def upsert_rooms(room_names: List[str]):
        for room_name in room_names:
            ExcalidrawRoom.objects.get_or_create(
                room_name=room_name, defaults={
                    'room_created_by': request.user,
                    'room_consumer': lti_tool,
                    'room_course_id': get_course_id(message_launch_data)
                })

    mode = request.POST.get("mode")
    resource = DeepLinkResource().set_url(absolute_reverse(request, 'lti:launch'))

    if mode == "classroom":
        room_name = get_room_name(request, message_launch_data)
        upsert_rooms([room_name])
        resource.set_title(title).set_custom_params({'mode': mode, 'room': room_name})

    elif mode == "group":
        n_groups = int(request.POST.get('n-groups'))
        room_names = [make_room_name(24) for _ in range(n_groups)]
        upsert_rooms(room_names)
        resource.set_title(title + " – " + _("Group Assignment"))\
            .set_custom_params({'mode': mode, 'rooms': ",".join(room_names)})

    elif mode == "single":
        resource.set_title(title + " – " + _("Single Person Assignment"))\
            .set_custom_params({'mode': mode, 'room_prefix': make_room_name(16)})

    return HttpResponse(message_launch.get_deep_link().output_response_form([resource]))


@require_POST
def lti_launch(request: HttpRequest):
    """
    Implements the LTI launch. It logs the user in and
    redirects them according to the requested launch type.
    """
    # parse and verify the data that was passed by the LTI consumer
    tool_conf = DjangoDbToolConf()
    launch_data_storage = DjangoCacheDataStorage()
    message_launch = DjangoMessageLaunch(
        request, tool_conf, launch_data_storage=launch_data_storage)
    message_launch_data = cast(dict, message_launch.get_launch_data())

    # log the user in. if this is the user's first visit, save them to the database before.
    # 1) extract user information from the data passed by the consumer
    username = get_ext_data(message_launch_data).get('user_username') # type: ignore
    username = issuer_namespaced_username(message_launch_data['iss'], username)
    user_full_name = message_launch_data.get('name', '')

    lti_tool = get_lti_tool(tool_conf, message_launch_data)

    # 2) get / create the user from the db and log them in
    UserModel = get_user_model()
    user, user_mod = UserModel.objects.get_or_create(username=username)
    if not user.first_name:
        user.first_name = user_full_name
        user_mod = True
    if user.registered_via_id != lti_tool.pk:
        # needed if tool was re-registered. otherwise trying
        # to save the user would raise an IngrityError.
        user.registered_via = lti_tool
        user_mod = True
    if user_mod:
        user.save()
    login(request, user)

    if message_launch.is_deep_link_launch():
        return render(request, 'ltiapi/configure.html', {
            'launch_id': message_launch.get_launch_id(),
        })

    if message_launch.is_data_privacy_launch():
        # TODO: implement data privacy screen. (not as urgent. moodle does not support this anyway.)
        #       see issue #17
        raise NotImplementedError()

    if message_launch.is_resource_launch():
        # every endpoint needs to do an auth check
        request.session.set_expiry(0)
        allowed_course_ids = request.session.get('course_ids', [])
        course_id = get_course_id(message_launch_data)
        allowed_course_ids.append(course_id)
        request.session['course_ids'] = allowed_course_ids

        # classroom is default for legacy db entry support
        custom_data = get_custom_launch_data(message_launch_data)
        mode = custom_data.get('mode', 'classroom')

        if mode == "classroom":
            room_name = get_room_name(request, message_launch_data)
            room_uri = absolute_reverse(request, 'collab:room', kwargs={'room_name': room_name})
            room = get_object_or_404(ExcalidrawRoom, room_name=room_name)
            # join the room if the user is allowed to access it.
            if not async_to_sync(user_is_authorized)(user, room, request.session):
                return HttpResponseForbidden("You are not allowed to access this room.")
            return redirect(room_uri)

        if mode == "group":
            return render(request, 'ltiapi/choose_group.html', {
                'rooms': custom_data.get('rooms').split(",")
            })

        if mode == "single":
            # XXX: users from the same course can edit boards of other users from that course
            room_name = get_user_room_name(custom_data['room_prefix'], user)
            room_uri = absolute_reverse(request, 'collab:room', kwargs={'room_name': room_name})
            room, created = ExcalidrawRoom.objects.get_or_create(room_name=room_name)
            if created:
                room.room_created_by=request.user
                room.room_consumer=lti_tool
                room.room_course_id=course_id
                room.save()
            elif not async_to_sync(user_is_authorized)(user, room, request.session):
                return HttpResponseForbidden("You are not allowed to access this room.")
            return redirect(room_uri)

        return HttpResponseBadRequest("Invalid assignment type.")

    return HttpResponseBadRequest('Unknown or unsupported message type provided.')

lti_launch.csrf_exempt = True
lti_launch.xframe_options_exempt = True
# The launch can only be triggered with a valid JWT issued by a registered platform.
