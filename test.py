import requests

API_KEY = "sk-ewgiaPC3A6pPDYHwR8siVA"
URL = "https://api.gpt.mws.ru/v1/models"

headers = {
    "Authorization": f"Bearer {API_KEY}",
}

response = requests.get(URL, headers=headers)

if response.status_code == 200:
    models = response.json()["data"]
    for model in models:
        print(f"Модель: {model['id']}, owned_by: {model['owned_by']}")
else:
    print(f"Ошибка: {response.text}")