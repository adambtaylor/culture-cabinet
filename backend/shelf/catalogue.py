from dataclasses import dataclass


@dataclass(frozen=True)
class Card:
    title: str
    category: str
    description: str


# Shared invented works, with no profile preferences or interaction state.
CARDS = (
    Card("The Paper Observatory", "Fictional film", "A mapmaker charts a sky made of paper."),
    Card("Mosslight Station", "Fictional book", "A quiet station waits for a train of gardens."),
    Card("The Clockwork Lagoon", "Fictional series", "Tiny boats follow an impossible clock."),
)


TARGET_IDS = tuple('00000000-0000-4000-8000-00000000010' + str(i) for i in (1, 2, 3))
