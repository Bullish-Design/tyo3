"""Class hierarchy for navigation and hierarchy tests."""


class Animal:
    """Base class for all animals."""

    def __init__(self, name: str) -> None:
        self.name = name

    def speak(self) -> str:
        """Make a sound."""
        return f"{self.name} makes a sound."

    def move(self) -> str:
        """Move around."""
        return f"{self.name} moves."


class Mammal(Animal):
    """A mammal — warm-blooded animal."""

    def __init__(self, name: str, has_fur: bool = True) -> None:
        super().__init__(name)
        self.has_fur = has_fur

    def feed_young(self) -> str:
        """Feed the young with milk."""
        return f"{self.name} feeds young with milk."


class Bird(Animal):
    """A bird — can fly."""

    def __init__(self, name: str, wingspan: float) -> None:
        super().__init__(name)
        self.wingspan = wingspan

    def fly(self) -> str:
        """Fly through the air."""
        return f"{self.name} flies with wingspan {self.wingspan}m."


class Dog(Mammal):
    """A domestic dog."""

    def __init__(self, name: str, breed: str) -> None:
        super().__init__(name, has_fur=True)
        self.breed = breed

    def speak(self) -> str:
        return f"{self.name} barks!"

    def fetch(self, item: str) -> str:
        """Fetch an item."""
        return f"{self.name} fetches the {item}."


class Cat(Mammal):
    """A domestic cat."""

    def __init__(self, name: str, color: str) -> None:
        super().__init__(name, has_fur=True)
        self.color = color

    def speak(self) -> str:
        return f"{self.name} meows."

    def purr(self) -> str:
        """Purr contentedly."""
        return f"{self.name} purrs."


class Eagle(Bird):
    """An eagle — majestic bird of prey."""

    def __init__(self, name: str, wingspan: float, prey: str) -> None:
        super().__init__(name, wingspan)
        self.prey = prey

    def hunt(self) -> str:
        """Hunt for prey."""
        return f"{self.name} hunts for {self.prey}."
