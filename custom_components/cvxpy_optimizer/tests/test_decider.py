from cvxpy_optimizer.decider import Decider



def test_decider_negative_price_snu():
    decider = Decider()
    # Negative price should trigger SNU if charging
    result = decider.decide(p_grid=1, p_charge=1, p_discharge=0, price=-0.5)
    assert result["charger_priority"] == "Solar First (SNU)"
    assert result["output_mode"] == "Solar First (SUB)"


def test_decider_positive_price_cso():
    decider = Decider()
    # Positive price + charging should trigger CSO
    result = decider.decide(p_grid=1, p_charge=1, p_discharge=0, price=0.5)
    assert result["charger_priority"] == "Solar + Utility (CSO)"


def test_decider_discharging_sbu():
    decider = Decider()
    # Discharging should trigger SBU
    result = decider.decide(p_grid=-1, p_charge=0, p_discharge=1, price=0.5)
    assert result["output_mode"] == "Solar+Battery First (SBU)"


def test_decider_no_activity_sub():
    decider = Decider()
    # No charging/discharging should trigger SUB
    result = decider.decide(p_grid=1, p_charge=0, p_discharge=0, price=0.5)
    assert result["output_mode"] == "Solar First (SUB)"


def test_decider_no_utility_charging_oso():
    decider = Decider()
    # No charging activity should trigger OSO (per logic in decider)
    result = decider.decide(p_grid=0, p_charge=0, p_discharge=0, price=0.5)
    assert result["charger_priority"] == "Solar Only (OSO)"
