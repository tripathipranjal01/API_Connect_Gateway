import json
import secrets
import base64
import asyncio
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import requests
from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = '9f169df1-3bb8-439f-bb00-f269b50c1dcf'  # Replace with your HubSpot app client ID
CLIENT_SECRET = '4897d1f6-dd97-46ad-92d0-0e71025cbf69'  # Replace with your HubSpot app client secret
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
SCOPE = 'crm.objects.contacts.read'

AUTH_URL = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPE}&response_type=code'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)
    return f'{AUTH_URL}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))

    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(encoded_state)

    original_state = state_data['state']
    user_id = state_data['user_id']
    org_id = state_data['org_id']

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    if not saved_state or original_state != json.loads(saved_state)['state']:
        raise HTTPException(status_code=400, detail='State mismatch.')

    async with httpx.AsyncClient() as client:
        token_resp, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded'
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )

    token_data = token_resp.json()
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(token_data), expire=600)

    close_window_script = """
    <html>
        <script>window.close();</script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')
    return json.loads(credentials)

def create_integration_item_metadata_object(contact) -> IntegrationItem:
    return IntegrationItem(
        id=contact.get('id'),
        type='Contact',
        name=contact.get('properties', {}).get('firstname', 'Unknown') + ' ' +
             contact.get('properties', {}).get('lastname', ''),
        creation_time=contact.get('properties', {}).get('createdate'),
        last_modified_time=contact.get('properties', {}).get('lastmodifieddate'),
        url=f"https://app.hubspot.com/contacts/{contact.get('portalId')}/contact/{contact.get('id')}"
    )

async def get_items_hubspot(credentials) -> list[IntegrationItem]:
    access_token = credentials.get("access_token")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    contacts_url = "https://api.hubapi.com/crm/v3/objects/contacts"
    params = {"limit": 10, "properties": "firstname,lastname,createdate,lastmodifieddate"}

    response = requests.get(contacts_url, headers=headers, params=params)

    integration_items = []
    if response.status_code == 200:
        contacts = response.json().get("results", [])
        for contact in contacts:
            item = create_integration_item_metadata_object(contact)
            integration_items.append(item)

    print("HubSpot Integration Items:", integration_items)
    return integration_items
