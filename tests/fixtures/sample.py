"""Sample module for testing the Python code extractor."""
import os
from pathlib import Path


MY_VAR = "hello"
API_KEY = os.getenv("API_KEY")


class Animal:
    """A base animal class."""

    species = "unknown"

    def speak(self) -> str:
        return ""


class Dog(Animal):
    """A dog."""

    def speak(self) -> str:
        return self.bark()

    def bark(self) -> str:
        return "woof"


def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}"


async def fetch_data(url: str) -> dict:
    """Fetch data from a URL."""
    result = greet(url)
    return {"url": url, "data": result}
