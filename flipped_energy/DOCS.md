# Flipped Energy

This app installs the Flipped Energy integration into Home Assistant and keeps it up to date.

Flipped Energy shows your plan's rates, the wholesale price and your meter's usage and cost. It does not show how much power your home is using right now: your meter sends its readings to Flipped a day or more later, so usage and cost start from yesterday.

## Quickest: paste your token first

1. Open **Configuration**, paste a token from **APIs and MCPs** in the Flipped portal into **Flipped token**, and save.
2. Select **Start** on the **Info** tab.

The app installs Flipped Energy, restarts Home Assistant and sets it up with your token. The **Log** tab shows each step.

## Or set it up yourself

1. Select **Start**. The app installs Flipped Energy and restarts Home Assistant.
2. When Home Assistant is back, go to **Settings** > **Devices & services** > **Add integration**, search for **Flipped Energy** and paste your token.

## Energy dashboard

Flipped Energy brings in your meter's usage and cost history. Meter data reaches Flipped a day or more late, so the newest day or two fill in later.

1. Go to **Settings** > **Dashboards** > **Energy** and select **Add grid connection**.
2. **Energy imported from grid**: **Flipped Energy Grid Import**.
3. **Cost tracking**: **Use an entity tracking the total costs** > **Flipped Energy Usage Cost**.
4. With solar panels: **Energy exported to grid**: **Flipped Energy Solar Export**, and **Export compensation**: **Use an entity tracking the total compensation** > **Flipped Energy Feed-in Credit**. Only do this if you also have a solar production sensor from your inverter; without one the dashboard shows your usage wrongly.
5. Select **Save**.

The cost excludes your daily supply charge, so it is lower than your bill.

## Updates

Leave the app running. When you update the app in **Settings** > **Updates**, it installs the new Flipped Energy version and restarts Home Assistant.

What Flipped Energy provides: https://github.com/flipped-energy/home-assistant
