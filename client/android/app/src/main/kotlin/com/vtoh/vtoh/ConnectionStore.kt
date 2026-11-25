package com.vtoh.vtoh

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class ConnectionStore(context: Context) {
    private val prefs = context.getSharedPreferences("vtoh_connection", Context.MODE_PRIVATE)
    private val alias = "vtoh_connection_key"

    private fun keyStore() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun read(): Map<String, String>? {
        val saved = prefs.getString("data", null) ?: return null
        val blob = Base64.decode(saved, Base64.NO_WRAP)
        val key = keyStore().getKey(alias, null) as? SecretKey
            ?: error("Connection key is missing")
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, blob.copyOfRange(0, 12)))
        val data = JSONObject(cipher.doFinal(blob.copyOfRange(12, blob.size)).toString(Charsets.UTF_8))
        return mapOf("address" to data.getString("address"), "key" to data.getString("key"))
    }

    fun write(address: String, accessKey: String) {
        val key = keyStore().getKey(alias, null) as? SecretKey
            ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
                .apply {
                    init(KeyGenParameterSpec.Builder(
                        alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
                    ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
                }.generateKey()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val data = JSONObject().put("address", address).put("key", accessKey)
        val blob = cipher.iv + cipher.doFinal(data.toString().toByteArray(Charsets.UTF_8))
        check(prefs.edit().putString("data", Base64.encodeToString(blob, Base64.NO_WRAP)).commit())
    }
}
