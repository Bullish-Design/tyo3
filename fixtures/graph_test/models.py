class Base:
    def save(self) -> None:
        pass


class User(Base):
    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    def save(self) -> None:
        print(f"Saving {self.name}")


MAX_USERS: int = 100
