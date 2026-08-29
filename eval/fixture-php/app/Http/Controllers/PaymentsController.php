<?php

namespace App\Http\Controllers;

/**
 * Handlers for the fixture payments API.
 *
 * Each returns [status, body] so a generated test can call it directly without
 * booting the framework, for the same reason the other fixtures avoid their web
 * frameworks: a judge must be able to run the evaluation from a clean checkout.
 */
class PaymentsController
{
    public function createPayout(array $body, array $headers = []): array
    {
        if (empty($headers['Idempotency-Key'])) {
            return [400, $this->error('missing_idempotency_key', 'Idempotency-Key header is required')];
        }
        if (empty($body['reference'])) {
            return [400, $this->error('missing_reference', 'reference is required')];
        }
        return [201, $this->payout($body)];
    }

    public function getPayout(string $id): array
    {
        return [200, $this->payout(['amount' => '150000', 'currency' => 'NGN', 'reference' => 'ref_123'], $id)];
    }

    public function createRefund(array $body): array
    {
        return [201, [
            'id' => 'rf_01HZX',
            'payoutId' => $body['payoutId'] ?? null,
            'status' => 'pending',
            'amount' => $body['amount'] ?? null,
        ]];
    }

    public function getBalance(): array
    {
        return [200, ['available' => '2450000', 'ledger' => '2500000', 'currency' => 'NGN']];
    }

    public function listTransactions(int $page = 1, int $perPage = 25): array
    {
        if ($page < 1) {
            $page = 1;
        }
        if ($perPage < 1) {
            $perPage = 25;
        }
        if ($perPage > 100) {
            $perPage = 100;
        }
        return [200, ['data' => [], 'page' => $page, 'perPage' => $perPage, 'total' => 0]];
    }

    public function createCustomer(array $body): array
    {
        if (empty($body['email'])) {
            return [400, $this->error('missing_email', 'email is required')];
        }
        return [201, [
            'id' => 'cus_01HZX',
            'email' => $body['email'],
            'firstName' => $body['firstName'] ?? null,
            'lastName' => $body['lastName'] ?? null,
            'phone' => $body['phone'] ?? null,
            'kycTier' => 0,
            'createdAt' => '2026-08-29T10:00:00Z',
        ]];
    }

    public function verifyBvn(array $body): array
    {
        if (strlen((string) ($body['bvn'] ?? '')) !== 11) {
            return [400, $this->error('invalid_bvn', 'bvn must be 11 digits')];
        }
        return [200, ['verified' => true, 'firstName' => 'Ada', 'lastName' => 'Okafor', 'tier' => 1]];
    }

    public function listBanks(): array
    {
        return [200, [
            ['code' => '044', 'name' => 'Access Bank', 'slug' => 'access-bank'],
            ['code' => '058', 'name' => 'Guaranty Trust Bank', 'slug' => 'gtb'],
        ]];
    }

    private function payout(array $body, string $id = 'po_01HZX'): array
    {
        return [
            'id' => $id,
            'status' => 'pending',
            'amount' => $body['amount'],
            'currency' => $body['currency'],
            'fee' => '5000',
            'reference' => $body['reference'],
            'createdAt' => '2026-08-29T10:00:00Z',
        ];
    }

    private function error(string $code, string $message): array
    {
        return ['success' => false, 'code' => $code, 'message' => $message];
    }
}
