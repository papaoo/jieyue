# -*- coding: utf-8 -*-
"""
===================================
股票数据服务层
===================================

职责：
1. 封装股票数据获取逻辑
2. 提供实时行情和历史数据接口
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from src.repositories.stock_repo import StockRepository

logger = logging.getLogger(__name__)


class StockService:
    """
    股票数据服务
    
    封装股票数据获取的业务逻辑
    """
    
    def __init__(self):
        """初始化股票数据服务"""
        self.repo = StockRepository()
    
    def get_realtime_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票实时行情
        
        Args:
            stock_code: 股票代码
            
        Returns:
            实时行情数据字典
        """
        try:
            # 调用数据获取器获取实时行情
            from data_provider.base import DataFetcherManager
            
            manager = DataFetcherManager()
            quote = manager.get_realtime_quote(stock_code)
            
            if quote is None:
                logger.warning(f"获取 {stock_code} 实时行情失败")
                return None
            
            # UnifiedRealtimeQuote 是 dataclass，使用 getattr 安全访问字段
            # 字段映射: UnifiedRealtimeQuote -> API 响应
            # - code -> stock_code
            # - name -> stock_name
            # - price -> current_price
            # - change_amount -> change
            # - change_pct -> change_percent
            # - open_price -> open
            # - high -> high
            # - low -> low
            # - pre_close -> prev_close
            # - volume -> volume
            # - amount -> amount
            return {
                "stock_code": getattr(quote, "code", stock_code),
                "stock_name": getattr(quote, "name", None),
                "current_price": getattr(quote, "price", 0.0) or 0.0,
                "change": getattr(quote, "change_amount", None),
                "change_percent": getattr(quote, "change_pct", None),
                "open": getattr(quote, "open_price", None),
                "high": getattr(quote, "high", None),
                "low": getattr(quote, "low", None),
                "prev_close": getattr(quote, "pre_close", None),
                "volume": getattr(quote, "volume", None),
                "amount": getattr(quote, "amount", None),
                "update_time": datetime.now().isoformat(),
            }
            
        except ImportError:
            logger.warning("DataFetcherManager 未找到，使用占位数据")
            return self._get_placeholder_quote(stock_code)
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}", exc_info=True)
            return None
    
    def get_history_data(
        self,
        stock_code: str,
        period: str = "daily",
        days: int = 30
    ) -> Dict[str, Any]:
        """
        获取股票历史行情
        
        Args:
            stock_code: 股票代码
            period: K 线周期 (daily/weekly/monthly)
            days: 获取天数
            
        Returns:
            历史行情数据字典
            
        Raises:
            ValueError: 当 period 不是 daily 时抛出（weekly/monthly 暂未实现）
        """
        # 验证 period 参数，只支持 daily
        if period != "daily":
            raise ValueError(
                f"暂不支持 '{period}' 周期，目前仅支持 'daily'。"
                "weekly/monthly 聚合功能将在后续版本实现。"
            )

        import math
        from datetime import timezone

        def finite_number(value: Any) -> Optional[float]:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) else None

        response = {
            "stock_code": stock_code,
            "stock_name": None,
            "period": period,
            "data": [],
            "source": None,
            "status": "unavailable",
            "warnings": ["daily-not-realtime", "adjustment-unknown", "freshness-not-verified"],
            "adjustment": None,
        }
        try:
            # 调用数据获取器获取历史数据
            from data_provider.base import DataFetcherManager
            
            manager = DataFetcherManager()
            df, source = manager.get_daily_data(stock_code, days=days)
            response["source"] = source if isinstance(source, str) and source.strip() else None

            if df is None or df.empty:
                response["status"] = "empty"
                response["warnings"].append("empty-result")
                return response

            # 获取股票名称
            response["stock_name"] = manager.get_stock_name(stock_code)

            # 转换为响应格式
            data = []
            invalid_price_rows = 0
            missing_optional = set()
            for _, row in df.iterrows():
                prices = {field: finite_number(row.get(field)) for field in ("open", "high", "low", "close")}
                if any(value is None or value <= 0 for value in prices.values()):
                    invalid_price_rows += 1
                    continue
                if (
                    prices["high"] < max(prices["open"], prices["close"], prices["low"])
                    or prices["low"] > min(prices["open"], prices["close"], prices["high"])
                ):
                    invalid_price_rows += 1
                    continue
                date_val = row.get("date")
                if hasattr(date_val, "strftime"):
                    date_str = date_val.strftime("%Y-%m-%d")
                else:
                    date_str = str(date_val)
                
                optional_values = {
                    "volume": finite_number(row.get("volume")),
                    "amount": finite_number(row.get("amount")),
                    "change_percent": finite_number(row.get("pct_chg")),
                }
                missing_optional.update(field for field, value in optional_values.items() if value is None)
                data.append({
                    "date": date_str,
                    **prices,
                    **optional_values,
                })

            if invalid_price_rows:
                response["warnings"].append(f"invalid-ohlc-rows-skipped:{invalid_price_rows}")
            if missing_optional:
                response["warnings"].append(f"optional-fields-missing:{','.join(sorted(missing_optional))}")
            if response["source"] is None:
                response["warnings"].append("source-unknown")
            response["data"] = data
            response["status"] = (
                "partial" if invalid_price_rows or missing_optional or response["source"] is None else "available"
            )
            return response

        except Exception as e:
            logger.error("获取历史数据失败: %s", type(e).__name__)
            response["warnings"].append("source-unavailable")
            return response
        finally:
            # Request completion time, not the market observation/bar timestamp.
            response["fetched_at"] = datetime.now(timezone.utc).isoformat()
    
    def _get_placeholder_quote(self, stock_code: str) -> Dict[str, Any]:
        """
        获取占位行情数据（用于测试）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            占位行情数据
        """
        return {
            "stock_code": stock_code,
            "stock_name": f"股票{stock_code}",
            "current_price": 0.0,
            "change": None,
            "change_percent": None,
            "open": None,
            "high": None,
            "low": None,
            "prev_close": None,
            "volume": None,
            "amount": None,
            "update_time": datetime.now().isoformat(),
        }
