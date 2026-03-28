const axios = require('axios');
const { ethers } = require('ethers');

class PearApi {
  constructor(apiUrl) {
    this.apiUrl = apiUrl;
    this.tokens = new Map(); // wallet -> { accessToken, refreshToken, expiresAt }
  }

  async authenticate(wallet, privateKey) {
    try {
      // Step 1: Get EIP712 message to sign
      const { data: eip712Data } = await axios.get(`${this.apiUrl}/auth/eip712-message`, {
        params: { address: wallet }
      });

      // Step 2: Sign the message
      const signer = new ethers.Wallet(privateKey);
      const signature = await signer.signTypedData(
        eip712Data.domain,
        eip712Data.types,
        eip712Data.message || eip712Data.value
      );

      // Step 3: Login
      const { data: loginData } = await axios.post(`${this.apiUrl}/auth/login`, {
        type: 'eip712',
        address: wallet,
        signature,
        message: eip712Data
      });

      this.tokens.set(wallet.toLowerCase(), {
        accessToken: loginData.accessToken,
        refreshToken: loginData.refreshToken,
        expiresAt: Date.now() + (loginData.expiresIn || 3600) * 1000
      });

      return true;
    } catch (error) {
      console.error(`Auth failed for ${wallet}:`, error.response?.data || error.message);
      return false;
    }
  }

  async authenticateWithApiKey(wallet, apiKey) {
    try {
      const { data: loginData } = await axios.post(`${this.apiUrl}/auth/login`, {
        type: 'apiKey',
        address: wallet,
        apiKey
      });

      this.tokens.set(wallet.toLowerCase(), {
        accessToken: loginData.accessToken,
        refreshToken: loginData.refreshToken,
        expiresAt: Date.now() + (loginData.expiresIn || 3600) * 1000
      });

      return true;
    } catch (error) {
      console.error(`API key auth failed for ${wallet}:`, error.response?.data || error.message);
      return false;
    }
  }

  async refreshTokenIfNeeded(wallet) {
    const walletLower = wallet.toLowerCase();
    const tokenData = this.tokens.get(walletLower);
    if (!tokenData) return false;

    // Refresh if within 5 minutes of expiry
    if (tokenData.expiresAt - Date.now() > 5 * 60 * 1000) return true;

    try {
      const { data } = await axios.post(`${this.apiUrl}/auth/refresh`, {
        refreshToken: tokenData.refreshToken
      });

      this.tokens.set(walletLower, {
        accessToken: data.accessToken,
        refreshToken: data.refreshToken,
        expiresAt: Date.now() + (data.expiresIn || 3600) * 1000
      });
      return true;
    } catch {
      this.tokens.delete(walletLower);
      return false;
    }
  }

  getHeaders(wallet) {
    const tokenData = this.tokens.get(wallet.toLowerCase());
    if (!tokenData) throw new Error(`No auth token for wallet ${wallet}`);
    return { Authorization: `Bearer ${tokenData.accessToken}` };
  }

  async getPositions(wallet) {
    await this.refreshTokenIfNeeded(wallet);
    try {
      const { data } = await axios.get(`${this.apiUrl}/positions`, {
        headers: this.getHeaders(wallet)
      });
      return data;
    } catch (error) {
      console.error(`Failed to get positions for ${wallet}:`, error.response?.data || error.message);
      return null;
    }
  }

  async getTradeHistory(wallet, limit = 50) {
    await this.refreshTokenIfNeeded(wallet);
    try {
      const { data } = await axios.get(`${this.apiUrl}/trade-history`, {
        headers: this.getHeaders(wallet),
        params: { limit }
      });
      return data;
    } catch (error) {
      console.error(`Failed to get trade history for ${wallet}:`, error.response?.data || error.message);
      return null;
    }
  }

  async getOpenOrders(wallet) {
    await this.refreshTokenIfNeeded(wallet);
    try {
      const { data } = await axios.get(`${this.apiUrl}/orders/open`, {
        headers: this.getHeaders(wallet)
      });
      return data;
    } catch (error) {
      console.error(`Failed to get open orders for ${wallet}:`, error.response?.data || error.message);
      return null;
    }
  }

  async getAccount(wallet) {
    await this.refreshTokenIfNeeded(wallet);
    try {
      const { data } = await axios.get(`${this.apiUrl}/accounts`, {
        headers: this.getHeaders(wallet)
      });
      return data;
    } catch (error) {
      console.error(`Failed to get account for ${wallet}:`, error.response?.data || error.message);
      return null;
    }
  }

  async getPortfolio(wallet) {
    await this.refreshTokenIfNeeded(wallet);
    try {
      const { data } = await axios.get(`${this.apiUrl}/portfolio`, {
        headers: this.getHeaders(wallet)
      });
      return data;
    } catch (error) {
      console.error(`Failed to get portfolio for ${wallet}:`, error.response?.data || error.message);
      return null;
    }
  }

  isAuthenticated(wallet) {
    return this.tokens.has(wallet.toLowerCase());
  }
}

module.exports = PearApi;
