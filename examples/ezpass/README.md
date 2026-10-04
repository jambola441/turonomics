# EZPass fixtures

`transactions.csv` is the shape of a real NY EZPass account-activity download
— the column order, the leading space before a tag number, the negative
amounts for charges, the `PAYMENT` rows that have to be skipped — with
invented contents.

Tag numbers here start `999`. Real ones do not, and none is in this
repository: a transponder number identifies a tolling account, and this repo
is public. The fleet's bindings are set on the API service as

    EZPASS_TAGS=<car>=<tag>,<car>=<tag>

The license plates are the fleet's own, because the matcher keys on them and a
fixture with invented plates would not exercise it.
