"""Configure the backend key without putting it in shell history or source."""
from getpass import getpass
from backend.providers.credentials import save_api_key


if __name__ == '__main__':
    save_api_key(getpass('OpenCode API key: '))
    print('OpenCode key saved in Windows Credential Manager.')
