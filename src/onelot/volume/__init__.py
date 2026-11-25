"""The Volume Watch (US-089): the market's volume read from a basket of large stocks.

`indicator` is pure (numpy, pandas, scipy); `service` builds `/api/volume` from the desk's bars
and the store; `routes` registers the four routes. Nothing here can place an order.
"""
