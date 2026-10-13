import requests

client_id = "5097f651e74345f38c5f801cdfb8d528"
client_secret = "b331f56ea837494fb4c13b401c5b534c"
code = "a7fs7xzzbzt2qcs7"
redirect_uri = "https://oauth.yandex.ru/verification_code"

data = {
    "grant_type": "authorization_code",
    "code": code,
    "client_id": client_id,
    "client_secret": client_secret,
    "redirect_uri": redirect_uri
}

resp = requests.post("https://oauth.yandex.ru/token", data=data)
print(resp.json())
