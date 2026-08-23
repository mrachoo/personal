"""ABA routing number validation and a small directory of well-known US bank
routing numbers for the transfer form's live bank detection. The directory is
intentionally conservative — unknown-but-valid numbers are accepted with no
bank name rather than guessed at."""

ROUTING_BANKS = {
    "021000021": "JPMorgan Chase",
    "322271627": "JPMorgan Chase",
    "071000013": "JPMorgan Chase",
    "026009593": "Bank of America",
    "122000661": "Bank of America",
    "121000248": "Wells Fargo",
    "121042882": "Wells Fargo",
    "021000089": "Citibank",
    "026013673": "TD Bank",
    "011401533": "Citizens Bank",
    "043000096": "PNC Bank",
    "031101279": "The Bancorp Bank",
    "314074269": "USAA Federal Savings Bank",
    "256074974": "Navy Federal Credit Union",
    "061000104": "Truist Bank",
    "021001088": "HSBC Bank USA",
    "031176110": "Capital One",
    "091000022": "U.S. Bank",
}


def aba_checksum_valid(number):
    """Standard ABA checksum: 3-7-1 digit weighting, sum must be divisible by 10."""
    if len(number) != 9 or not number.isdigit():
        return False
    d = [int(c) for c in number]
    total = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return total % 10 == 0


def lookup_bank(number):
    """Returns (valid, bank_name_or_empty)."""
    if not aba_checksum_valid(number):
        return False, ""
    return True, ROUTING_BANKS.get(number, "")
