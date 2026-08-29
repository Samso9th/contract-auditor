<?php

use App\Http\Controllers\PaymentsController;
use Illuminate\Support\Facades\Route;

// The auditor extracts its route table from this file.
Route::prefix('v1')->group(function () {
    Route::post('/payouts', [PaymentsController::class, 'createPayout']);
    Route::get('/payouts/{id}', [PaymentsController::class, 'getPayout']);
    Route::post('/refunds', [PaymentsController::class, 'createRefund']);
    Route::get('/balance', [PaymentsController::class, 'getBalance']);
    Route::get('/transactions', [PaymentsController::class, 'listTransactions']);
    Route::post('/customers', [PaymentsController::class, 'createCustomer']);
    Route::post('/kyc/bvn', [PaymentsController::class, 'verifyBvn']);
    Route::get('/banks', [PaymentsController::class, 'listBanks']);
});
