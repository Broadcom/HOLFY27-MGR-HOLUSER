#!/bin/bash

# Define password length and special characters
PASS_LEN=16
SPECIAL_CHARS="!-"
CHAR_POOL='A-Za-z0-9!-'

# Variable to hold the final password, initialized to a fail state to start the loop
FINAL_PASS=""

# --- Function to Check Password Requirements ---
# Checks for the presence of the required character classes using grep.
check_strength() {
    local password="$1"

    # The chain returns exit code 0 (Success) only if ALL checks succeed.
    if  echo "$password" | grep -q '[[:lower:]]' && \
        echo "$password" | grep -q '[[:upper:]]' && \
        echo "$password" | grep -q '[[:digit:]]' && \
        echo "$password" | grep -q '[!-]'; then
        return 0 # Success: all required characters present
    else
        return 1 # Failure: missing at least one required character type
    fi
}

# The loop continues UNTIL the check_strength function returns success (exit code 0)
until check_strength "$FINAL_PASS"; do
	# 1. Generate the special character
	SPEC_CHAR=$(echo "$SPECIAL_CHARS" | fold -w1 | shuf | head -n1)

    # 2. Generate a base password one character shorter (Original Logic)
    # NOTE: Using /dev/urandom and tr here is often faster than openssl in a tight loop, 
    # but the original logic using openssl is kept as requested.
    BASE_PASS=$(openssl rand -base64 48 | tr -dc "$CHAR_POOL" | head -c $((PASS_LEN-1)))

    # 3. Determine a random position (Original Logic)
    # Range is 1 to PASS_LEN-1, ensuring the character is not the first.
    POS=$((RANDOM % (PASS_LEN-1) + 1))

    # 4. Insert the special character and set the FINAL_PASS candidate (Original Logic)
    FINAL_PASS="${BASE_PASS:0:$POS}${SPEC_CHAR}${BASE_PASS:$POS}"
done
	
echo "$FINAL_PASS"
