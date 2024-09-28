from typing import Annotated
import types
from async_dns.core import types as DNSType
from async_dns.resolver import ProxyResolver
from fastapi.responses import RedirectResponse
import httpx
import json
from fastapi import FastAPI, Request, Header
from fastapi.templating import Jinja2Templates
import textwrap
from html import escape as html_escape
from mixpanel import Mixpanel
import os
from ua_parser import user_agent_parser

redirect_home_to_url = os.getenv('REDIRECT_HOME_TO_URL')

if redirect_home_to_url is not None and not httpx.URL(redirect_home_to_url).is_absolute_url:
    raise Exception('REDIRECT_HOME_TO_URL must be an absolute URL')

mp_token = os.getenv('MIXPANEL_TOKEN')

if mp_token is None:
    raise Exception('MIXPANEL_TOKEN environment variable not set')

mp = Mixpanel(mp_token)

app = FastAPI()
templates = Jinja2Templates(directory='app/templates')

@app.get('/oembed/author/{author}/{url}')
async def get_oembed_author(author: str, url: str):
    return {
        'type': 'video',
        'author_name': author,
        'author_url': url,
        'provider_name': author,
        'provider_url': url,
    }

@app.get('/ping')
async def ping():
    return {'pong': 'it works!'}

@app.get('/')
async def home():
    if redirect_home_to_url is not None:
        return RedirectResponse(url=redirect_home_to_url)
    return RedirectResponse(url='https://bsky.app')

@app.get('/profile/{profile}')
async def get_profile_info(request: Request, profile: str, user_agent: Annotated[str | None, Header()] = None, client_ip: str = Header(None, alias='X-Real-IP')):
    is_telegram = 'Telegram' in user_agent
    is_discord = 'Discord' in user_agent
    is_ping_bot = 'Better Uptime Bot' in user_agent
    crawler = 'Telegram' if is_telegram else 'Discord' if is_discord else 'Ping Bot' if is_ping_bot else 'Other'
    parsed_user_agent = user_agent_parser.Parse(user_agent)
    
    did = None
    if profile.startswith('did:'):
        did = profile
    else:
        did = await resolve_handle(profile)
    
    pds, handle = await get_pds_and_handle(did)
    profile = await get_profile(pds, did)
    try:
        avatar = blob_url(pds, did, profile['value']['avatar']['ref']['$link'])
    except KeyError:
        avatar = None
    try:
        bio = profile['value']['description']
    except KeyError:
        bio = None
    try:
        display_name = profile['value']['displayName']
    except KeyError:
        display_name = None
    disregard = False
    try:
        values = profile['value']['labels']['values']
        for value in values:
            if value['val'] == '!no-unauthenticated':
                disregard = True
                break
    except KeyError:
        pass
    
    url = ('https://skychat.social/#profile/' if disregard else 'https://bsky.app/profile/') + did
    
    mp.track(client_ip, event_name='Profile Generated', properties={
        'crawler': crawler,
        'did': did,
        'ip': client_ip
    })
    
    if client_ip is None:
        client_ip = request.client.host
    
    return templates.TemplateResponse(
        request=request,
        name='profile.jinja',
        context={
            'disregard': disregard,
            'display_name': display_name,
            'handle': handle,
            'avatar': avatar,
            'bio': bio,
            'url': url,
            '$browser': parsed_user_agent['user_agent']['family'],
            '$device': parsed_user_agent['device']['family'],
            '$os': parsed_user_agent['os']['family'],
        }
    )

@app.get('/profile/{profile}/post/{rkey}')
async def get_post_info(request: Request, profile: str, rkey: str, user_agent: Annotated[str | None, Header()] = None, client_ip: str = Header(None, alias='X-Real-IP')):
    is_telegram = 'Telegram' in user_agent
    is_discord = 'Discord' in user_agent
    is_ping_bot = 'Better Uptime Bot' in user_agent
    crawler = 'Telegram' if is_telegram else 'Discord' if is_discord else 'Ping Bot' if is_ping_bot else 'Other'
    parsed_user_agent = user_agent_parser.Parse(user_agent)
    
    did = None
    if profile.startswith('did:'):
        did = profile
    else:
        did = await resolve_handle(profile)

    pds, handle = await get_pds_and_handle(did)
    profile = await get_profile(pds, did)
    try:
        display_name = profile['value']['displayName']
    except KeyError:
        display_name = None
    disregard = False
    try:
        values = profile['value']['labels']['values']
        for value in values:
            if value['val'] == '!no-unauthenticated':
                disregard = True
                break
    except KeyError:
        pass
    post = await get_post(pds, did, rkey)
    
    instant_view = None
    
    created_at = post['value']['createdAt']
    if is_telegram:
        instant_view = types.SimpleNamespace()
        bsky_thread = await get_bsky_post_thread(post['uri'])
        bsky_profile = await get_bsky_actor_profile(did)
        
        instant_view.reply_count = '💬 ' + str(bsky_thread['thread']['post']['replyCount'])
        instant_view.repost_count = '🔁 ' + str(bsky_thread['thread']['post']['repostCount'])
        instant_view.like_count = '❤️ ' + str(bsky_thread['thread']['post']['likeCount'])
        instant_view.quote_count = '🔃 ' + str(bsky_thread['thread']['post']['quoteCount'])
        
        instant_view.avatar = bsky_thread['thread']['post']['author']['avatar']
        instant_view.bio = profile['value']['description']
        instant_view.following = bsky_profile['followsCount']
        instant_view.followers = bsky_profile['followersCount']
        instant_view.posts = bsky_profile['postsCount']
        instant_view.author_url = ('https://skychat.social/#profile/' if disregard else 'https://bsky.app/profile/') + did
        
        instant_view.paragraphs = []
        for paragraph in post['value']['text'].split('\n'):
            if len(paragraph) < 1:
                continue
            instant_view.paragraphs.append(paragraph)
    
    image_urls = []
    video = None
    quote = ''
    if 'embed' in post['value']:
        for image in post['value']['embed'].get('images', []) + post['value']['embed'].get('media', {}).get('images', []):
            image_urls.append(blob_url(pds, did, image['image']['ref']['$link']))
        if 'video' in post['value']['embed']:
            video = types.SimpleNamespace()
            video.url = blob_url(pds, did, post['value']['embed']['video']['ref']['$link'])
            video.mime_type = post['value']['embed']['video']['mimeType']
            video.width = post['value']['embed']['aspectRatio']['width']
            video.height = post['value']['embed']['aspectRatio']['height']
        if 'record' in post['value']['embed']:
            record = post['value']['embed']['record']
            if 'uri' in record:
                uri_split = record['uri'].split('/')
            else:
                uri_split = record['record']['uri'].split('/')

            assert uri_split[3] == 'app.bsky.feed.post'
            quoted_pds, quoted_handle = await get_pds_and_handle(uri_split[2])
            quoted_post = await get_post(quoted_pds, uri_split[2], uri_split[4])
            quoted_profile = await get_profile(quoted_pds, uri_split[2])
            try:
                quoted_display_name = quoted_profile['value']['displayName']
            except KeyError:
                quoted_display_name = None
            quote = f'\n\n↘️ Quoting {quoted_display_name} (@{quoted_handle}):\n\n{quoted_post["value"]["text"]}' if 'value' in quoted_post else f'\n\nQuoting deleted skeet from {quoted_display_name} (@{quoted_handle})'
    
    reply = ''
    if 'reply' in post['value']:
        uri_split = post['value']['reply']['parent']['uri'].split('/')
        assert uri_split[3] == 'app.bsky.feed.post'
        _, replied_handle = await get_pds_and_handle(uri_split[2])
        reply = f'Reply to @{replied_handle}: '

    url = f'https://skychat.social/#thread/{did}/{rkey}' if disregard else f'https://bsky.app/profile/{did}/post/{rkey}'
    
    text = reply + post['value']['text'] + quote
    
    if is_discord and video is not None:
        text = textwrap.shorten(text, width=255, placeholder='...')
    
    if client_ip is None:
        client_ip = request.client.host
    
    mp.track(client_ip, event_name='Post Generated', properties={
        'crawler': crawler,
        'did': did,
        'rkey': rkey,
        'ip': client_ip,
        '$browser': parsed_user_agent['user_agent']['family'],
        '$device': parsed_user_agent['device']['family'],
        '$os': parsed_user_agent['os']['family'],
    })

    return templates.TemplateResponse(
        request=request, name='post.jinja', context={
            'disregard': disregard,
            'display_name': display_name,
            'handle': handle,
            'text': text,
            'image_urls': image_urls,
            'video': video,
            'url': url,
            'escaped_url': html_escape(url),
            'created_at': created_at,
            'instant_view': instant_view,
            'is_telegram': is_telegram,
            'is_discord': is_discord,
        }
    )
    
dns_resolver = ProxyResolver()
client = httpx.AsyncClient()
headers={'User-Agent': 'Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'}

class CannotResolveHandleException(Exception):
    pass

async def resolve_handle(handle):
    res, _ = await dns_resolver.query('_atproto.' + handle, DNSType.TXT)
    if len(res.an) >= 1:
        data = res.an[0].data.data
        assert data.startswith('did=did:')
        return data[4:]
    try:
        res = await client.get(f'https://{handle}/.well-known/atproto-did')
    except Exception:
        raise CannotResolveHandleException

    assert res.text.startswith('did:')
    return res.text

class InvalidDIDException(Exception):
    pass

async def get_pds_and_handle(did):
    if did.startswith('did:plc:'):
        doc = (await client.get(f'https://plc.directory/{did}', headers=headers)).json()
    elif did.startswith('did:web:'):
        doc = (await client.get(f'https://{did[8:]}/.well-known/did.json', headers=headers)).json()
    else:
        raise InvalidDIDException
    
    handle = None
    for aka in doc.get('alsoKnownAs', []):
        if aka.startswith('at://'):
            handle = aka[5:]
            break
    if not handle:
        raise InvalidDIDException
    
    pds = None
    for service in doc.get('service', []):
        if service.get('id', '') == '#atproto_pds' and service.get('type', '') == 'AtprotoPersonalDataServer':
            pds = service.get('serviceEndpoint')
            break
    if not pds:
        raise InvalidDIDException
    
    return pds, handle

async def get_profile(pds, did):
    return (await client.get(f'{pds}/xrpc/com.atproto.repo.getRecord?repo={did}&collection=app.bsky.actor.profile&rkey=self', headers=headers)).json()

async def get_post(pds, did, rkey):
    return (await client.get(f'{pds}/xrpc/com.atproto.repo.getRecord?repo={did}&collection=app.bsky.feed.post&rkey={rkey}', headers=headers)).json()

def blob_url(pds, did, cid):
    return f'{pds}/xrpc/com.atproto.sync.getBlob?did={did}&cid={cid}'

async def get_bsky_post_thread(uri):
    return (await client.get(f'https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?uri={uri}&depth=0&parentHeight=1', headers=headers)).json()

async def get_bsky_actor_profile(did):
    return (await client.get(f'https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={did}', headers=headers)).json()