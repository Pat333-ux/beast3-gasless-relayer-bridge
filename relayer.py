import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List
from fastapi import FastAPI, Request, HTTPException
from google.cloud import firestore
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

app = FastAPI(title="BEAST 3.0 Gasless Relayer & On-Chain Bridge", version="3.0.0")

# Environment & Chain Configuration
RPC_URL = os.environ.get("BEAST_RPC_URL", "https://sepolia.base.org")
CHAIN_ID = int(os.environ.get("BEAST_CHAIN_ID", "84532"))
LUCR_GOVERNANCE_ADDRESS = os.environ.get("LUCR_GOVERNANCE_ADDRESS", "0x0000000000000000000000000000000000000000")
RELAYER_PRIVATE_KEY = os.environ.get("BEAST_RELAYER_PRIVATE_KEY", "")
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

w3 = Web3(Web3.HTTPProvider(RPC_URL))
db = firestore.Client(project=GCP_PROJECT) if GCP_PROJECT else None

# Minimal ABI for LUCR Governance & Merkle Checkpoint Settlement
LUCR_RELAYER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "proposalId", "type": "uint256"},
            {"internalType": "address", "name": "voter", "type": "address"},
            {"internalType": "uint8", "name": "support", "type": "uint8"},
            {"internalType": "uint256", "name": "nonce", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "castVoteBySig",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "merkleRoot", "type": "bytes32"},
            {"internalType": "uint256", "name": "leafCount", "type": "uint256"},
        ],
        "name": "anchorConstitutionalRoot",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def build_eip712_vote_typed_data(proposal_id: int, voter: str, support: int, nonce: int, deadline: int) -> Dict[str, Any]:
    """Constructs the EIP-712 structured payload matching LUCR Governor & MetaMask signing."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Ballot": [
                {"name": "proposalId", "type": "uint256"},
                {"name": "voter", "type": "address"},
                {"name": "support", "type": "uint8"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "Ballot",
        "domain": {
            "name": "BEAST3-LUCR-Governor",
            "version": "3.0",
            "chainId": CHAIN_ID,
            "verifyingContract": Web3.to_checksum_address(LUCR_GOVERNANCE_ADDRESS),
        },
        "message": {
            "proposalId": proposal_id,
            "voter": Web3.to_checksum_address(voter),
            "support": support,
            "nonce": nonce,
            "deadline": deadline,
        },
    }


def broadcast_contract_tx(fn_call) -> str:
    """Signs and broadcasts an on-chain transaction using the dedicated Cloud Run relayer key."""
    if not RELAYER_PRIVATE_KEY:
        raise RuntimeError("BEAST_RELAYER_PRIVATE_KEY is not configured in Secret Manager.")

    relayer_acct = Account.from_key(RELAYER_PRIVATE_KEY)
    nonce = w3.eth.get_transaction_count(relayer_acct.address, "pending")
    gas_estimate = fn_call.estimate_gas({"from": relayer_acct.address})

    tx = fn_call.build_transaction({
        "chainId": CHAIN_ID,
        "from": relayer_acct.address,
        "nonce": nonce,
        "gas": int(gas_estimate * 1.2),
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei("0.1", "gwei"),
    })
    signed_tx = w3.eth.account.sign_transaction(tx, private_key=RELAYER_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return tx_hash.hex()


@app.get("/health")
async def health():
    relayer_address = Account.from_key(RELAYER_PRIVATE_KEY).address if RELAYER_PRIVATE_KEY else "unconfigured"
    return {
        "status": "operational",
        "service": "BEAST 3.0 Gasless Relayer Bridge",
        "chain_id": CHAIN_ID,
        "rpc_connected": w3.is_connected(),
        "relayer_wallet": relayer_address,
        "governor_contract": LUCR_GOVERNANCE_ADDRESS,
    }


@app.post("/relay/vote")
async def relay_gasless_vote(request: Request):
    """Verifies a member's off-chain MetaMask EIP-712 vote signature and relays it on-chain."""
    body = await request.json()
    proposal_id = int(body["proposal_id"])
    voter = Web3.to_checksum_address(body["voter"])
    support = int(body["support"])  # 0 = Against, 1 = For, 2 = Abstain
    nonce = int(body["nonce"])
    deadline = int(body["deadline"])
    signature = body["signature"]

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if now_ts > deadline:
        raise HTTPException(status_code=400, detail="EIP-712 vote signature has expired.")

    typed_data = build_eip712_vote_typed_data(proposal_id, voter, support, nonce, deadline)
    signable = encode_typed_data(full_message=typed_data)
    recovered_signer = Account.recover_message(signable, signature=signature)

    if recovered_signer.lower() != voter.lower():
        raise HTTPException(status_code=401, detail="Invalid EIP-712 signature: signer mismatch.")

    vote_id = f"{proposal_id}:{voter.lower()}:{nonce}"
    if db:
        vote_ref = db.collection("beast_relayed_votes").document(vote_id)
        if vote_ref.get().exists:
            return {"status": "already_relayed", "vote_id": vote_id}

    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(LUCR_GOVERNANCE_ADDRESS),
            abi=LUCR_RELAYER_ABI,
        )
        fn = contract.functions.castVoteBySig(
            proposal_id,
            voter,
            support,
            nonce,
            bytes.fromhex(signature.removeprefix("0x")),
        )
        tx_hash = broadcast_contract_tx(fn)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"On-chain relay execution failed: {exc}")

    if db:
        db.collection("beast_relayed_votes").document(vote_id).set({
            "proposal_id": proposal_id,
            "voter": voter,
            "support": support,
            "nonce": nonce,
            "tx_hash": tx_hash,
            "relayed_at": datetime.now(timezone.utc),
        })

    return {
        "status": "relayed_onchain",
        "vote_id": vote_id,
        "tx_hash": tx_hash,
    }


@app.post("/relay/anchor-roots")
async def anchor_unsettled_merkle_roots():
    """Fetches unanchored Merkle roots from Constitutional Policy Guard and settles them on-chain."""
    if not db:
        raise HTTPException(status_code=503, detail="Firestore binding required.")

    pending = list(
        db.collection("beast_merkle_roots")
        .where("anchored_onchain", "==", False)
        .limit(10)
        .stream()
    )

    if not pending:
        return {"status": "idle", "reason": "All constitutional Merkle roots are anchored on-chain."}

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(LUCR_GOVERNANCE_ADDRESS),
        abi=LUCR_RELAYER_ABI,
    )
    anchored: List[Dict[str, Any]] = []

    for doc in pending:
        data = doc.to_dict()
        root_hex = data["merkle_root"].removeprefix("0x")
        leaf_count = int(data["leaf_count"])

        try:
            fn = contract.functions.anchorConstitutionalRoot(bytes.fromhex(root_hex), leaf_count)
            tx_hash = broadcast_contract_tx(fn)
            doc.reference.update({
                "anchored_onchain": True,
                "anchor_tx_hash": tx_hash,
                "anchored_at": datetime.now(timezone.utc),
            })
            anchored.append({"merkle_root": f"0x{root_hex}", "tx_hash": tx_hash})
        except Exception as exc:
            doc.reference.update({"last_anchor_error": str(exc)})

    return {
        "status": "anchor_batch_complete",
        "anchored_count": len(anchored),
        "transactions": anchored,
    }
