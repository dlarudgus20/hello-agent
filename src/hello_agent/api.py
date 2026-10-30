import requests
from openai import OpenAI

from .auth import RESOURCE, get_access_token
from .errors import AppError


def create_client() -> OpenAI:
    return OpenAI(
        api_key=get_access_token,
        base_url=RESOURCE,
        max_retries=0,
    )


def list_models():
    token = get_access_token()

    response = requests.get(
        f"{RESOURCE}/models",
        headers={
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )
    response.raise_for_status()

    return [
        model
        for model in response.json()["models"]
        if model.get("visibility") == "list"
    ]


def ask(model, message):
    client = OpenAI(
        api_key=get_access_token,
        base_url=RESOURCE,
        max_retries=0,
    )

    completed = False

    with client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": message,
            }
        ],
        store=False,
        stream=True,
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)

            elif event.type == "response.completed":
                completed = True

            elif event.type in (
                "response.failed",
                "response.incomplete",
                "error",
            ):
                raise AppError(str(event))

    if not completed:
        raise AppError("Response stream did not complete")

    print()
