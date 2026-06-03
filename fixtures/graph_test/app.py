from models import MAX_USERS, User


def create_user(name: str) -> User:
    if len(name) > MAX_USERS:
        raise ValueError("Name too long")
    return User(name)


def main() -> None:
    user = create_user("Alice")
    user.save()
