#if __has_include("sqlite3.h")

#include "MeshService.h"
#include "RTC.h"
#include "SHA256.h"
#include "SfppFrame.h"
#include "SfppInternal.h"
#include "StoreForwardPlusPlus.h"
#include "UptimeClock.h"
#include "concurrency/LockGuard.h"
#include "mesh/CryptoEngine.h"
#include "mesh/Throttle.h"
#include "meshUtils.h"
#include "modules/RoutingModule.h"
#if !MESHTASTIC_EXCLUDE_STOREFORWARD
#include "modules/StoreForwardModule.h"
#endif

#include <algorithm>
#include <cstdio>
#include <iterator>
#include <utility>
#include <vector>

void StoreForwardPlusPlusModule::backfillSetIndex()
{
    uint32_t indexed = 0;

    // One batch per pass: the select finds rows the update then fills, so re-running it is how the
    // loop advances. A store written before these columns existed converges in chain_length/128 passes.
    while (true) {
        std::vector<std::pair<uint32_t, uint64_t>> derived;
        std::vector<std::vector<uint8_t>> hashes;

        sqlite3_reset(unindexedLinksStmt);
        while (sqlite3_step(unindexedLinksStmt) == SQLITE_ROW) {
            const uint8_t *hash = (const uint8_t *)sqlite3_column_blob(unindexedLinksStmt, 0);
            const int len = sqlite3_column_bytes(unindexedLinksStmt, 0);
            if (hash == nullptr || len <= 0)
                continue;
            hashes.emplace_back(hash, hash + len);
            derived.emplace_back(sfppsr::shortId(hash, len), sfppsr::checksumContribution(hash, len));
        }
        sqlite3_reset(unindexedLinksStmt);

        if (hashes.empty())
            break;

        for (size_t i = 0; i < hashes.size(); i++) {
            sqlite3_reset(setLinkSetIndexStmt);
            sqlite3_bind_int64(setLinkSetIndexStmt, 1, derived[i].first);
            sqlite3_bind_int64(setLinkSetIndexStmt, 2, (sqlite3_int64)derived[i].second);
            sqlite3_bind_blob(setLinkSetIndexStmt, 3, hashes[i].data(), hashes[i].size(), NULL);
            if (sqlite3_step(setLinkSetIndexStmt) != SQLITE_DONE) {
                LOG_ERROR("StoreForwardpp cannot index link: %s", sqlite3_errmsg(ppDb));
                sqlite3_reset(setLinkSetIndexStmt);
                return; // an update that does not stick would loop the select forever
            }
            indexed++;
        }
        sqlite3_reset(setLinkSetIndexStmt);
    }

    if (indexed > 0)
        LOG_INFO("StoreForwardpp indexed %u existing links for set reconciliation", indexed);
}

bool StoreForwardPlusPlusModule::buildBucketSummary(const uint8_t *root_hash, size_t root_hash_len, uint32_t bucket,
                                                    sfppsr::BucketSummary &out)
{
    out.clear();

    sqlite3_reset(bucketSummaryStmt);
    sqlite3_bind_int(bucketSummaryStmt, 1, root_hash_len);
    sqlite3_bind_blob(bucketSummaryStmt, 2, root_hash, root_hash_len, NULL);
    sqlite3_bind_int(bucketSummaryStmt, 3, (int)bucket);

    while (sqlite3_step(bucketSummaryStmt) == SQLITE_ROW) {
        out.add((pinsketch::Element)sqlite3_column_int64(bucketSummaryStmt, 0),
                (uint64_t)sqlite3_column_int64(bucketSummaryStmt, 1));
    }
    sqlite3_reset(bucketSummaryStmt);

    return out.count() > 0;
}

bool StoreForwardPlusPlusModule::collectBucketIds(const uint8_t *root_hash, size_t root_hash_len, uint32_t bucket,
                                                  std::vector<pinsketch::Element> &out)
{
    out.clear();
    sqlite3_reset(bucketIdsStmt);
    sqlite3_bind_int(bucketIdsStmt, 1, root_hash_len);
    sqlite3_bind_blob(bucketIdsStmt, 2, root_hash, root_hash_len, NULL);
    sqlite3_bind_int(bucketIdsStmt, 3, (int)bucket);

    while (sqlite3_step(bucketIdsStmt) == SQLITE_ROW)
        out.push_back((pinsketch::Element)sqlite3_column_int64(bucketIdsStmt, 0));
    sqlite3_reset(bucketIdsStmt);

    return !out.empty();
}

bool StoreForwardPlusPlusModule::collectBuckets(const uint8_t *root_hash, size_t root_hash_len, std::vector<uint32_t> &out)
{
    out.clear();
    sqlite3_reset(distinctBucketsStmt);
    sqlite3_bind_int(distinctBucketsStmt, 1, root_hash_len);
    sqlite3_bind_blob(distinctBucketsStmt, 2, root_hash, root_hash_len, NULL);
    while (sqlite3_step(distinctBucketsStmt) == SQLITE_ROW)
        out.push_back((uint32_t)sqlite3_column_int64(distinctBucketsStmt, 0));
    sqlite3_reset(distinctBucketsStmt);
    return !out.empty();
}

//
bool StoreForwardPlusPlusModule::addToScratch(link_object &lo)
{
    // push a message into the local chain DB
    // destination
    sqlite3_bind_int64(scratch_insert_stmt, 1, (sqlite3_int64)lo.to);
    // sender
    sqlite3_bind_int64(scratch_insert_stmt, 2, (sqlite3_int64)lo.from);
    // packet_id
    sqlite3_bind_int64(scratch_insert_stmt, 3, (sqlite3_int64)lo.id);
    // root_hash
    sqlite3_bind_blob(scratch_insert_stmt, 4, lo.root_hash, SFPP_HASH_SIZE, NULL);
    // encrypted_bytes
    sqlite3_bind_blob(scratch_insert_stmt, 5, lo.encrypted_bytes, lo.encrypted_len, NULL);
    // message_hash
    sqlite3_bind_blob(scratch_insert_stmt, 6, lo.message_hash, SFPP_HASH_SIZE, NULL);
    // rx_time
    sqlite3_bind_int(scratch_insert_stmt, 7, lo.rx_time);
    // payload
    sqlite3_bind_text(scratch_insert_stmt, 8, lo.payload.c_str(), lo.payload.length(), NULL);

    int res = sqlite3_step(scratch_insert_stmt);
    if (res != SQLITE_OK) {
        const char *_error_mesg = sqlite3_errmsg(ppDb);
        LOG_WARN("StoreForwardpp step %u, %s", res, _error_mesg);
    }
    sqlite3_reset(scratch_insert_stmt);
    return true;
}

bool StoreForwardPlusPlusModule::isInDB(const uint8_t *message_hash_bytes, size_t message_hash_len)
{
    if (message_hash_len < SFPP_SHORT_HASH_SIZE)
        return false;
    sqlite3_bind_int(checkDupMessageHash, 1, message_hash_len);
    sqlite3_bind_blob(checkDupMessageHash, 2, message_hash_bytes, message_hash_len, NULL);
    sqlite3_step(checkDupMessageHash);
    int numberFound = sqlite3_column_int(checkDupMessageHash, 0);
    sqlite3_reset(checkDupMessageHash);
    if (numberFound > 0)
        return true;
    return false;
}

bool StoreForwardPlusPlusModule::isInScratch(uint8_t *message_hash_bytes, size_t message_hash_len)
{
    if (message_hash_len < SFPP_SHORT_HASH_SIZE)
        return false;
    sqlite3_bind_int(checkScratch, 1, message_hash_len);
    sqlite3_bind_blob(checkScratch, 2, message_hash_bytes, message_hash_len, NULL);
    sqlite3_step(checkScratch);
    int numberFound = sqlite3_column_int(checkScratch, 0);
    sqlite3_reset(checkScratch);
    if (numberFound > 0)
        return true;
    return false;
}

void StoreForwardPlusPlusModule::removeFromScratch(const uint8_t *message_hash_bytes, size_t message_hash_len)
{
    printBytes("StoreForwardpp removing from scratch: ", message_hash_bytes, message_hash_len);
    sqlite3_bind_int(removeScratch, 1, message_hash_len);
    sqlite3_bind_blob(removeScratch, 2, message_hash_bytes, message_hash_len, NULL);
    sqlite3_step(removeScratch);
    sqlite3_reset(removeScratch);
}

void StoreForwardPlusPlusModule::updatePayload(uint8_t *message_hash_bytes, size_t message_hash_len, std::string payload)
{
    LOG_WARN("StoreForwardpp updatePayload");
    sqlite3_bind_text(updatePayloadStmt, 1, payload.c_str(), payload.length(), NULL);
    sqlite3_bind_int(updatePayloadStmt, 2, message_hash_len);
    sqlite3_bind_blob(updatePayloadStmt, 3, message_hash_bytes, message_hash_len, NULL);
    auto res = sqlite3_step(updatePayloadStmt);
    if (res != SQLITE_OK) {
        const char *_error_mesg = sqlite3_errmsg(ppDb);
        LOG_WARN("StoreForwardpp updatePayloadStmt step error %u, %s", res, _error_mesg);
    }
    sqlite3_reset(updatePayloadStmt);
}

StoreForwardPlusPlusModule::link_object StoreForwardPlusPlusModule::getFromScratch(uint8_t *message_hash_bytes, size_t hash_len)
{
    link_object lo;

    sqlite3_bind_int(fromScratchByHashStmt, 1, hash_len);
    sqlite3_bind_blob(fromScratchByHashStmt, 2, message_hash_bytes, hash_len, NULL);
    auto res = sqlite3_step(fromScratchByHashStmt);
    if (res != SQLITE_ROW && res != SQLITE_OK) {
        const char *_error_mesg = sqlite3_errmsg(ppDb);
        LOG_WARN("StoreForwardpp fromScratchByHashStmt step error %u, %s", res, _error_mesg);
    }
    lo.to = sqlite3_column_int(fromScratchByHashStmt, 0);
    lo.from = sqlite3_column_int(fromScratchByHashStmt, 1);
    lo.id = sqlite3_column_int(fromScratchByHashStmt, 2);

    uint8_t *encrypted_bytes = (uint8_t *)sqlite3_column_blob(fromScratchByHashStmt, 3);
    lo.encrypted_len = sqlite3_column_bytes(fromScratchByHashStmt, 3);
    memcpy(lo.encrypted_bytes, encrypted_bytes, lo.encrypted_len);

    uint8_t *message_hash = (uint8_t *)sqlite3_column_blob(fromScratchByHashStmt, 4);
    lo.message_hash_len = sqlite3_column_bytes(fromScratchByHashStmt, 4);
    memcpy(lo.message_hash, message_hash, SFPP_HASH_SIZE);

    lo.rx_time = sqlite3_column_int(fromScratchByHashStmt, 5);

    uint8_t *root_hash = (uint8_t *)sqlite3_column_blob(fromScratchByHashStmt, 6);
    lo.root_hash_len = SFPP_HASH_SIZE;
    memcpy(lo.root_hash, root_hash, SFPP_HASH_SIZE);
    lo.payload =
        std::string((char *)sqlite3_column_text(fromScratchByHashStmt, 7), sqlite3_column_bytes(fromScratchByHashStmt, 7));
    sqlite3_reset(fromScratchByHashStmt);
    return lo;
}

bool StoreForwardPlusPlusModule::lookUpFullRootHash(uint8_t *partial_root_hash, size_t partial_root_hash_len,
                                                    uint8_t *full_root_hash)
{
    printBytes("StoreForwardpp partial_root_hash", partial_root_hash, partial_root_hash_len);
    sqlite3_bind_int(getFullRootHashStmt, 1, partial_root_hash_len);
    sqlite3_bind_blob(getFullRootHashStmt, 2, partial_root_hash, partial_root_hash_len, NULL);
    sqlite3_step(getFullRootHashStmt);
    uint8_t *tmp_root_hash = (uint8_t *)sqlite3_column_blob(getFullRootHashStmt, 0);
    if (tmp_root_hash) {
        LOG_DEBUG("StoreForwardpp Found full root hash!");
        memcpy(full_root_hash, tmp_root_hash, SFPP_HASH_SIZE);
        sqlite3_reset(getFullRootHashStmt);
        return true;
    }
    sqlite3_reset(getFullRootHashStmt);
    return false;
}

uint32_t StoreForwardPlusPlusModule::getScratchCount(uint8_t *root_hash, size_t root_hash_len)
{
    sqlite3_bind_int(getScratchCountStmt, 1, root_hash_len);
    sqlite3_bind_blob(getScratchCountStmt, 2, root_hash, root_hash_len, NULL);

    int res = sqlite3_step(getScratchCountStmt);
    if (res != SQLITE_OK && res != SQLITE_DONE && res != SQLITE_ROW) {
        LOG_ERROR("StoreForwardpp getScratchCountStmt sqlite error %u, %s", res, sqlite3_errmsg(ppDb));
    }
    uint32_t count = sqlite3_column_int(getScratchCountStmt, 0);
    sqlite3_reset(getScratchCountStmt);
    return count;
}

void StoreForwardPlusPlusModule::pruneScratchQueue()
{
    // Very little utility in holding on to very old scratch messages
    sqlite3_bind_int(pruneScratchQueueStmt, 1, time(nullptr) - 60 * 60 * 6);
    int res = sqlite3_step(pruneScratchQueueStmt);
    if (res != SQLITE_OK && res != SQLITE_DONE) {
        LOG_ERROR("StoreForwardpp Prune Scratch sqlite error %u, %s", res, sqlite3_errmsg(ppDb));
    }
    sqlite3_reset(pruneScratchQueueStmt);
    scratch_timed_out += sqlite3_changes(ppDb);
}

bool StoreForwardPlusPlusModule::insertCanonObject(uint32_t to, uint32_t from, uint32_t packetId, const uint8_t *cipher,
                                                   size_t cipherLen, const uint8_t *hash, uint32_t rxTime,
                                                   const uint8_t *root_hash, uint32_t bucket)
{
    if (isInDB(hash, SFPP_HASH_SIZE))
        return true;

    sqlite3_reset(chain_insert_stmt);
    sqlite3_bind_int64(chain_insert_stmt, 1, (sqlite3_int64)to);
    sqlite3_bind_int64(chain_insert_stmt, 2, (sqlite3_int64)from);
    sqlite3_bind_int64(chain_insert_stmt, 3, (sqlite3_int64)packetId);
    sqlite3_bind_blob(chain_insert_stmt, 4, root_hash, SFPP_HASH_SIZE, SQLITE_STATIC);
    sqlite3_bind_blob(chain_insert_stmt, 5, cipher, (int)cipherLen, SQLITE_STATIC);
    sqlite3_bind_blob(chain_insert_stmt, 6, hash, SFPP_HASH_SIZE, SQLITE_STATIC);
    sqlite3_bind_int64(chain_insert_stmt, 7, (sqlite3_int64)rxTime);
    sqlite3_bind_text(chain_insert_stmt, 8, "", 0, SQLITE_STATIC);
    sqlite3_bind_int64(chain_insert_stmt, 9, (sqlite3_int64)sfppsr::shortId(hash, SFPP_HASH_SIZE));
    sqlite3_bind_int64(chain_insert_stmt, 10, (sqlite3_int64)sfppsr::checksumContribution(hash, SFPP_HASH_SIZE));
    sqlite3_bind_int(chain_insert_stmt, 11, (int)sfppsr::contentBucket(hash, SFPP_HASH_SIZE));

    const int res = sqlite3_step(chain_insert_stmt);
    sqlite3_reset(chain_insert_stmt);
    if (res != SQLITE_OK && res != SQLITE_DONE) {
        LOG_ERROR("StoreForwardpp cannot store a canonical object: %s", sqlite3_errmsg(ppDb));
        return false;
    }
    return true;
}

bool StoreForwardPlusPlusModule::storeRecoveredObject(const sfppframe::Replay &replay, const uint8_t *root_hash, uint32_t bucket)
{
    uint8_t hash[SFPP_HASH_SIZE];
    sfppframe::messageHash(replay.ciphertext.data(), replay.ciphertext.size(), replay.to, replay.from, replay.id, hash);
    if (isInDB(hash, sizeof(hash))) {
        removeFromScratch(hash, SFPP_HASH_SIZE);
        return true;
    }

    const uint32_t now = getValidTime(RTCQuality::RTCQualityNTP, true);
    const uint32_t rxTime = replay.heardAgoS <= now ? now - replay.heardAgoS : now;
    if (!insertCanonObject(replay.to, replay.from, replay.id, replay.ciphertext.data(), replay.ciphertext.size(), hash, rxTime,
                           root_hash, bucket))
        return false;
    // The object is canonical now, so a scratch copy (a writer-missed offer) is no longer needed.
    removeFromScratch(hash, SFPP_HASH_SIZE);
    return true;
}

#endif // has include sqlite3
