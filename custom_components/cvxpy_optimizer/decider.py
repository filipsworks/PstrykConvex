class Decider:
    def __init__(self):
        pass

    def decide(self, p_grid, p_charge, p_discharge, price):
        """
        Determines the modes based on optimized power flows and price.
        """
        # Charger Priority logic
        if price < 0:
            charger_priority = "Solar First (SNU)"
        elif p_charge > 0:
            # If we are charging, it's either CSO or SNU
            # Since price >= 0 here, it must be CSO
            charger_priority = "Solar + Utility (CSO)"
        else:
            # If not charging, user might want OSO? 
            # But if the optimizer doesn't charge, we can say OSO.
            # However, the decision is usually for 'how to behave'.
            charger_priority = "Solar Only (OSO)"

        # Output Mode logic (Discharging)
        if p_discharge > 0:
            # If discharging, we are in SBU mode
            output_mode = "Solar+Battery First (SBU)"
        else:
            # If not discharging, we are using utility power
            output_mode = "Solar First (SUB)"

        return {
            "output_mode": output_mode,
            "charger_priority": charger_priority
        }

if __name__ == "__main__":
    decider = Decider()
    # Test 1: Charging with negative price
    print("Test 1 (Negative Price):", decider.decide(p_grid=5, p_charge=5, p_discharge=0, price=-0.5))
    # Test 2: Charging with positive price
    print("Test 2 (Positive Price, charging):", decider.decide(p_grid=5, p_charge=5, p_discharge=0, price=0.5))
    # Test 3: Discharging
    print("Test 3 (Discharging):", decider.decide(p_grid=-2, p_charge=0, p_discharge=2, price=0.5))
    # Test 4: No activity
    print("Test 4 (No activity):", decelse_res := decider.decide(p_grid=0, p_charge=0, p_discharge=0, price=0.5))
