from app.main import run_demo_checkout


def test_run_demo_checkout_returns_discounted_total() -> None:
    charged = run_demo_checkout()
    # (299 + 99) * 0.9 = 358.2
    assert charged == 358.2
