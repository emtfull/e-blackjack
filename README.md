# Blackjack Prediction Lab

The player site and the backend are separate. The official table is the only page a player should open. Removing a path from that site does not reveal the lab, the mechanic shoes, or the live felt.

Five gadgets play together on the official table because seats and cards live on the shared `/api/room` server (Redis on Vercel). Each player opens the same URL, picks the same Live or Machine source, and sits in a free seat. A new browser tab is another player only when that tab uses a different seat — the table itself is shared.

The backend pages share one browser. The prediction lab records the dealt order, Blackjack Live Table plays that order, and e Blackjack keeps five full shoes for tables 2–6. The official table reads those cards from the backend server; it does not host the backend pages.

| Page | Address | What it is |
| --- | --- | --- |
| Official table | `https://ebj-delta.vercel.app/` | Shared player table. Live or Machine is chosen here |
| Prediction Lab | `https://ebj-backend.vercel.app/` | Enter cards and run the next-card forecast |
| Blackjack Live Table | `https://ebj-backend.vercel.app/player-simulation.html` | The felt. Table 1 plays the entered sequence |
| e Blackjack | `https://ebj-backend.vercel.app/mechanic-tables.html` | Backend shoes for tables 2–6 |

Local multiplayer host: `python3 server.py 8080`, then every gadget opens `http://<this-computer-ip>:8080/`.

The felt page also holds the five e Blackjack tables. Use the buttons across the top to move between them. A table change is allowed only between hands.

## Blackjack Live Table

This is table 1.

It plays the dealt sequence in the order entered on the prediction lab. The length is however many cards were logged. A short list is a real shoe. A full 8-deck shoe is 416 cards. The table does not pad a short list up to 416.

The felt reads `history` from the lab save. Adding cards on the end keeps the deal pointer where it is. Shortening the list, or changing an earlier card, starts that table again at the first card.

Paste and Import JSON on the felt also load into Blackjack Live Table. Those actions replace the live sequence for this browser.

## e Blackjack

This is tables 2, 3, 4, 5, and 6.

Each table has its own 416-card shoe: 8 decks, 13 ranks, 4 suits, eight copies of every exact card. The casino page builds those shoes with a fair shuffle. Opening e Blackjack the first time builds all five if a complete set is not already saved. **Build all 5 shoes** replaces every table. **Reshuffle this shoe** replaces only that table.

Blackjack Live Table is not one of these shoes. Rebuilding e Blackjack does not change the prediction sequence.

On the felt, upcoming e Blackjack cards stay hidden unless Peek Next Card is on. Cards already dealt on the current visit stay visible.

## How the pages stay together

Each page saves in this browser and announces the change to other open tabs.

| Save | Written by | Read by |
| --- | --- | --- |
| `bj_prediction_lab_v1` | Prediction Lab | Blackjack Live Table |
| `bj_casino_tables_v1` | e Blackjack | Felt tables 2–6 |
| `bj_player_table_v1` | Felt | Felt, on the next visit |

The shared channel is `bj_shoe_sync`.

- A lab save tells the felt to reload Blackjack Live Table.
- An e Blackjack save tells the felt to reload tables 2–6.
- **Sync Backend** reloads the live sequence on demand.
- **Sync e Blackjack** reloads the five casino shoes on demand.

A shoe pointer for tables 2–6 resets only when that table’s shoe actually changes. Finish the current hand before switching tables, restarting a shoe, or syncing a replacement shoe.

## Prediction Lab

The lab keeps an 8-deck ledger. Enter a card and it leaves the remaining shoe, then the active `predictNextCard(state)` runs before the next entry. The function receives one argument, `state`, and returns `exactCard`, `topRank`, `category`, `confidence`, and `reason`.

The dealt line under the input is the same order Blackjack Live Table will play. Expand the log when you want the forecast that was locked before each card.

Preset buttons replace the editor. Apply & Run Code saves the editor as the active forecast. Presets:

1. Card Counter (Hi-Lo + remaining cards)
2. Shuffle / Key-Card Mirror
3. Slug & Side-Bet Auditor
4. Team Ensemble Blend
5. QRP Trigger Bias

Preset 5 reads the SPLIT, TIMEOUT, and MAINT checkboxes from `state.triggers`. Any checked trigger weights 10, J, Q, K, and A by 1.5. Buttons 1–4 do not use those checkboxes. After a checkbox change, run Predict Next Card again.

Undo Last Card, New 8-Deck Shoe, Export JSON, Export CSV, and Import JSON belong to the lab ledger.

## Felt rules

The felt uses the same payouts on every table.

| Result | Pay |
| --- | --- |
| Blackjack on the first two cards | 3 to 2 |
| Ordinary win | 1 to 1 |
| Insurance against a dealer ace | 2 to 1 |
| Even money | 1 to 1 |
| Pair on the first two cards | 11 to 1 |
| First three cards are suited 6-7-8 or suited 7-7-7 | 3 to 1 |

The starting bankroll is $10,000. Reset $10,000 restores it. Dealer soft 17 can be set to stand or hit. Main, Pair, and Jackpot bets are placed before Deal Hand. Hit, Stand, Double Down, and Split are available during the hand when the rules allow them.

## Files

```
player/index.html             Official table, the only player page
player/assets                 Table artwork
backend/index.html            Prediction Lab
backend/player-simulation.html
backend/mechanic-tables.html  e Blackjack shoe backend
backend/serve.py              Backend server and the card-source store
backend/vercel.json           Sends the old lab filename to /
```

The old address `/blackjack-prediction-lab.html` redirects to `/` on the backend server.
